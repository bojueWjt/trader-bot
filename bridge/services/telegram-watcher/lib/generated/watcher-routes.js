// 仅保留 never_allowed，待 WGW-1.0.2 后删除
// Independent method-agnostic deny list pending generator support.
module.exports = Object.freeze({
  neverAllowed: Object.freeze([
  {
    "inner_path": "/api/config",
    "methods": "*"
  },
  {
    "inner_path": "/api/login/*",
    "methods": "*"
  },
  {
    "inner_path": "/api/login/qr/*",
    "methods": "*"
  },
  {
    "inner_path": "/",
    "methods": "*"
  },
  {
    "inner_path": "/index.html",
    "methods": "*"
  },
  {
    "inner_path": "/healthz",
    "methods": "*"
  }
].map(Object.freeze)),
});
