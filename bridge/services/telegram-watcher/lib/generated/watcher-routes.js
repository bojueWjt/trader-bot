// WGW-1.0 route artifact. Temporary handoff: replace with the development generator in C-1.
// Runtime must never parse YAML; tests compare every identity/method/path row with the source.
module.exports = {
  "contractVersion": "WGW-1.0",
  "routes": [
    {
      "id": "gw.status.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/status",
      "write": false
    },
    {
      "id": "gw.trading_messages.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/messages",
      "write": false
    },
    {
      "id": "gw.briefings.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/briefings",
      "write": false
    },
    {
      "id": "gw.media.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/media/{filename}",
      "write": false
    },
    {
      "id": "gw.media.head",
      "identity": "gateway",
      "method": "HEAD",
      "path": "/media/{filename}",
      "write": false
    },
    {
      "id": "gw.disconnect.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/disconnect",
      "write": true
    },
    {
      "id": "gw.reconnect.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/reconnect",
      "write": true
    },
    {
      "id": "gw.orders.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/orders",
      "write": false
    },
    {
      "id": "gw.orders_active.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/orders/active",
      "write": false
    },
    {
      "id": "gw.dialogs.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/dialogs",
      "write": false
    },
    {
      "id": "gw.groups.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/groups",
      "write": true
    },
    {
      "id": "gw.accounts.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/accounts",
      "write": false
    },
    {
      "id": "gw.account.put",
      "identity": "gateway",
      "method": "PUT",
      "path": "/api/trading/accounts/{account_id}",
      "write": true
    },
    {
      "id": "gw.account.delete",
      "identity": "gateway",
      "method": "DELETE",
      "path": "/api/trading/accounts/{account_id}",
      "write": true
    },
    {
      "id": "gw.channels.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/channels",
      "write": false
    },
    {
      "id": "gw.channels.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/trading/channels",
      "write": true
    },
    {
      "id": "gw.channel.delete",
      "identity": "gateway",
      "method": "DELETE",
      "path": "/api/trading/channels/{channel_id}",
      "write": true
    },
    {
      "id": "gw.risks.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/trading/risks",
      "write": false
    },
    {
      "id": "gw.risks.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/trading/risks",
      "write": true
    },
    {
      "id": "gw.risk.delete",
      "identity": "gateway",
      "method": "DELETE",
      "path": "/api/trading/risks/{symbol}",
      "write": true
    },
    {
      "id": "gw.price_alerts.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/price-alerts",
      "write": false
    },
    {
      "id": "gw.price_alerts.post",
      "identity": "gateway",
      "method": "POST",
      "path": "/api/price-alerts",
      "write": true
    },
    {
      "id": "gw.price_alert.delete",
      "identity": "gateway",
      "method": "DELETE",
      "path": "/api/price-alerts/{alert_id}",
      "write": true
    },
    {
      "id": "gw.price_monitor_status.get",
      "identity": "gateway",
      "method": "GET",
      "path": "/api/price-monitor/status",
      "write": false
    },
    {
      "id": "snap.config_snapshot.get",
      "identity": "snapshot",
      "method": "GET",
      "path": "/api/trading/config-snapshot",
      "write": false
    },
    {
      "id": "br.healthz.get",
      "identity": "browser",
      "method": "GET",
      "path": "/healthz",
      "write": false
    },
    {
      "id": "br.static_root.get",
      "identity": "browser",
      "method": "GET",
      "path": "/",
      "write": false
    },
    {
      "id": "br.static_index.get",
      "identity": "browser",
      "method": "GET",
      "path": "/index.html",
      "write": false
    },
    {
      "id": "br.status.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/status",
      "write": false
    },
    {
      "id": "br.config.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/config",
      "write": false
    },
    {
      "id": "br.login_start.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/login/start",
      "write": false
    },
    {
      "id": "br.login_code.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/login/code",
      "write": false
    },
    {
      "id": "br.login_password.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/login/password",
      "write": false
    },
    {
      "id": "br.login_status.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/login/status",
      "write": false
    },
    {
      "id": "br.login_qr_start.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/login/qr/start",
      "write": false
    },
    {
      "id": "br.login_qr_status.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/login/qr/status",
      "write": false
    },
    {
      "id": "br.login_qr_password.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/login/qr/password",
      "write": false
    },
    {
      "id": "br.groups.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/groups",
      "write": true
    },
    {
      "id": "br.dialogs.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/dialogs",
      "write": false
    },
    {
      "id": "br.messages_ring.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/messages",
      "write": false
    },
    {
      "id": "br.disconnect.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/disconnect",
      "write": true
    },
    {
      "id": "br.reconnect.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/reconnect",
      "write": true
    },
    {
      "id": "br.media.get",
      "identity": "browser",
      "method": "GET",
      "path": "/media/{filename}",
      "write": false
    },
    {
      "id": "br.media.head",
      "identity": "browser",
      "method": "HEAD",
      "path": "/media/{filename}",
      "write": false
    },
    {
      "id": "br.accounts.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/accounts",
      "write": false
    },
    {
      "id": "br.accounts.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/trading/accounts",
      "write": true
    },
    {
      "id": "br.account.put",
      "identity": "browser",
      "method": "PUT",
      "path": "/api/trading/accounts/{account_id}",
      "write": true
    },
    {
      "id": "br.account.delete",
      "identity": "browser",
      "method": "DELETE",
      "path": "/api/trading/accounts/{account_id}",
      "write": true
    },
    {
      "id": "br.channels.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/channels",
      "write": false
    },
    {
      "id": "br.channels.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/trading/channels",
      "write": true
    },
    {
      "id": "br.channel.delete",
      "identity": "browser",
      "method": "DELETE",
      "path": "/api/trading/channels/{channel_id}",
      "write": true
    },
    {
      "id": "br.risks.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/risks",
      "write": false
    },
    {
      "id": "br.risks.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/trading/risks",
      "write": true
    },
    {
      "id": "br.risk.delete",
      "identity": "browser",
      "method": "DELETE",
      "path": "/api/trading/risks/{symbol}",
      "write": true
    },
    {
      "id": "br.orders.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/orders",
      "write": false
    },
    {
      "id": "br.orders_active.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/orders/active",
      "write": false
    },
    {
      "id": "br.briefings.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/briefings",
      "write": false
    },
    {
      "id": "br.trading_messages.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/trading/messages",
      "write": false
    },
    {
      "id": "br.price_monitor_status.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/price-monitor/status",
      "write": false
    },
    {
      "id": "br.price_alerts.get",
      "identity": "browser",
      "method": "GET",
      "path": "/api/price-alerts",
      "write": false
    },
    {
      "id": "br.price_alerts.post",
      "identity": "browser",
      "method": "POST",
      "path": "/api/price-alerts",
      "write": true
    },
    {
      "id": "br.price_alert.delete",
      "identity": "browser",
      "method": "DELETE",
      "path": "/api/price-alerts/{alert_id}",
      "write": true
    },
    {
      "id": "br.price_alerts_by_order.delete",
      "identity": "browser",
      "method": "DELETE",
      "path": "/api/price-alerts/order/{order_id}",
      "write": true
    }
  ],
  "pathParams": {
    "account_id": {
      "gateway_pattern": "^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$",
      "browser_pattern": "^[^\\u0000-\\u001F\\u007F]{1,128}$"
    },
    "channel_id": {
      "gateway_pattern": "^-?[0-9]{1,24}$",
      "browser_pattern": "^-?[0-9]{1,24}$"
    },
    "symbol": {
      "gateway_pattern": "^[A-Z0-9]{2,32}$",
      "browser_pattern": "^[A-Za-z0-9]{1,32}$"
    },
    "filename": {
      "gateway_pattern": "^[0-9]{10,16}-[0-9]{1,20}\\.(jpg|jpeg|png|gif|webp)$",
      "browser_pattern": "^[0-9]{10,16}-[0-9]{1,20}\\.[A-Za-z0-9.+-]{1,127}$"
    },
    "alert_id": {
      "gateway_pattern": "^[1-9][0-9]{0,18}$",
      "browser_pattern": "^[1-9][0-9]{0,18}$"
    },
    "order_id": {
      "gateway_pattern": null,
      "browser_pattern": "^[1-9][0-9]{0,18}$"
    }
  },
  "actorHeaders": {
    "actor": "X-Watcher-Actor",
    "actor_pattern": "^app:(system_observer|viewer|risk_admin|reviewer)$",
    "fingerprint": "X-Watcher-Token-Fingerprint",
    "fingerprint_pattern": "^[0-9a-f]{12}$"
  },
  "secretKeyPattern": {
    "pattern": "(api[_-]?key|api[_-]?secret|apisecret|api[_-]?hash|api[_-]?id|token|session|password|phone)",
    "flags": "i"
  },
  "neverAllowed": [
    "/api/config",
    "/api/login/*",
    "/api/login/qr/*",
    "/",
    "/index.html",
    "/healthz"
  ]
};
