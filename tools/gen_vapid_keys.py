"""Web Push 通知用の VAPID 鍵ペアを作る。

    python tools/gen_vapid_keys.py

表示された 2 行を config.yaml の notifications に貼り付けてください。
秘密鍵（vapid_private_key）は公開しないこと。鍵を作り直すと、スマホ側で通知の登録をやり直す必要があります。
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main():
    key = ec.generate_private_key(ec.SECP256R1())
    private_der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    public_raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    print("# config.yaml の notifications: の下に貼り付けてください")
    print(f'  vapid_public_key: "{b64url(public_raw)}"')
    print(f'  vapid_private_key: "{b64url(private_der)}"')


if __name__ == "__main__":
    main()
