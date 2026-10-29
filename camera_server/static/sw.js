self.addEventListener('push', function (event) {
  const options = { body: '新しい通知があります。', icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png' };
  let title = '通知';
  if (event.data) {
    const data = event.data.json();
    title = data.title || '通知';
    options.body = data.body || '新しいイベントが発生しました。';
    if (data.image) options.image = data.image;   // 通知にサムネイルを表示
  }
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});

self.addEventListener('fetch', function (event) {
  // キャッシュ戦略が必要ならここに書く
});
