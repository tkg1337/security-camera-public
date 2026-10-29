"""パスキー（WebAuthn）による管理者ログイン。auth.enabled: true のときだけ登録される。"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re

from flask import Flask, jsonify, request, session

from .config import Config


def to_base64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("utf-8")


def from_base64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def safe_username(name):
    """ファイル名に使うので、英数字と _ . - だけ許可する。"""
    if not name or not re.fullmatch(r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,63}", name):
        return None
    return name


def register_passkey_routes(app: Flask, cfg: Config):
    from webauthn import (generate_authentication_options, generate_registration_options,
                          verify_authentication_response, verify_registration_response)
    from webauthn.helpers.options_to_json import options_to_json
    from webauthn.helpers.structs import (AttestationConveyancePreference, AuthenticationCredential,
                                          AuthenticatorAssertionResponse, AuthenticatorAttestationResponse,
                                          AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          PublicKeyCredentialType, RegistrationCredential,
                                          UserVerificationRequirement)

    a = cfg.auth
    credentials_dir = cfg.credentials_dir
    credentials_dir.mkdir(parents=True, exist_ok=True)

    def credential_path(username):
        return credentials_dir / f"{username}.json"

    @app.route("/register/options", methods=["POST"])
    def register_options():
        data = request.get_json(silent=True) or {}
        username = safe_username(data.get("username"))
        if not username:
            return jsonify({"error": "ユーザー名は英数字と _ . - で入力してください"}), 400
        code = str(data.get("password") or "")
        if not a.invite_code or not hmac.compare_digest(code.encode(), a.invite_code.encode()):
            return jsonify({"error": "招待コードが違います"}), 403

        user_id = os.urandom(16)
        session["user_id"] = to_base64url(user_id)
        session["username"] = username
        options = generate_registration_options(
            rp_id=a.rp_id, rp_name=a.rp_name,
            user_name=username, user_id=user_id, user_display_name=username,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.PREFERRED),
        )
        session["challenge"] = to_base64url(options.challenge)
        public_key = json.loads(options_to_json(options))
        public_key["challenge"] = to_base64url(options.challenge)
        public_key["user"]["id"] = to_base64url(options.user.id)
        return jsonify(public_key)

    @app.route("/register", methods=["POST"])
    def register():
        credential = request.get_json(silent=True) or {}
        try:
            reg_cred = RegistrationCredential(
                id=credential["id"],
                raw_id=from_base64url(credential["rawId"]),
                type=credential["type"],
                response=AuthenticatorAttestationResponse(
                    client_data_json=from_base64url(credential["response"]["clientDataJSON"]),
                    attestation_object=from_base64url(credential["response"]["attestationObject"]),
                ),
                authenticator_attachment=credential.get("authenticatorAttachment"),
            )
            verification = verify_registration_response(
                credential=reg_cred,
                expected_challenge=from_base64url(session["challenge"]),
                expected_rp_id=a.rp_id,
                expected_origin=a.rp_origin,
                require_user_verification=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"登録エラー: {e}")
            return f"登録失敗: {e}", 400

        cred_data = {
            "credential_id": to_base64url(verification.credential_id),
            "public_key": to_base64url(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "user_handle": session["user_id"],
            "username": session["username"],
        }
        with open(credential_path(session["username"]), "w", encoding="utf-8") as f:
            json.dump(cred_data, f, indent=2)
        print("認証情報保存完了")
        return "登録完了"

    @app.route("/login/options", methods=["POST"])
    def login_options():
        data = request.get_json(silent=True) or {}
        username = safe_username(data.get("username"))
        if not username:
            return jsonify({"error": "ユーザー名が必要です"}), 400
        if not credential_path(username).exists():
            return jsonify({"error": "ユーザーが見つかりません"}), 404
        with open(credential_path(username), "r", encoding="utf-8") as f:
            stored = json.load(f)

        options = generate_authentication_options(
            rp_id=a.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(
                id=from_base64url(stored["credential_id"]), type=PublicKeyCredentialType.PUBLIC_KEY)],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        session["challenge"] = to_base64url(options.challenge)
        session["username_attempt"] = username
        public_key = json.loads(options_to_json(options))
        public_key["challenge"] = to_base64url(options.challenge)
        return jsonify(public_key)

    @app.route("/login", methods=["POST"])
    def login():
        credential = request.get_json(silent=True) or {}
        username = session.pop("username_attempt", None)
        if not username or not credential_path(username).exists():
            return "ログイン失敗: 登録された認証情報が見つかりません", 400
        with open(credential_path(username), "r", encoding="utf-8") as f:
            stored = json.load(f)
        try:
            resp = credential["response"]
            auth_cred = AuthenticationCredential(
                id=credential["id"],
                raw_id=from_base64url(credential["rawId"]),
                type=credential["type"],
                response=AuthenticatorAssertionResponse(
                    client_data_json=from_base64url(resp["clientDataJSON"]),
                    authenticator_data=from_base64url(resp["authenticatorData"]),
                    signature=from_base64url(resp["signature"]),
                    user_handle=from_base64url(resp["userHandle"]) if resp.get("userHandle") else None,
                ),
                authenticator_attachment=credential.get("authenticatorAttachment"),
            )
            verification = verify_authentication_response(
                credential=auth_cred,
                expected_challenge=from_base64url(session["challenge"]),
                expected_rp_id=a.rp_id,
                expected_origin=a.rp_origin,
                credential_public_key=from_base64url(stored["public_key"]),
                credential_current_sign_count=stored["sign_count"],
                require_user_verification=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"ログイン失敗: {e}")
            session["is_admin"] = False
            return f"ログイン失敗: {e}", 400

        stored["sign_count"] = verification.new_sign_count
        with open(credential_path(username), "w", encoding="utf-8") as f:
            json.dump(stored, f, indent=2)
        session["is_admin"] = True
        session["logged_in_user"] = username
        print(f"ログイン成功: {username}")
        return "ログイン成功"
