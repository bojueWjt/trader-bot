module.exports = {
  apps: [{
    name: 'telegram-watcher',
    script: './server.js',
    cwd: '/Users/balen/git/telegram-watcher',
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    max_memory_restart: '500M',
    env: {
      NODE_ENV: 'production',
      HERMES_PROFILE: 'trader',
      HERMES_ACCEPT_HOOKS: '1',
      WATCHER_HOST: '127.0.0.1',
      HERMES_SIGNAL_STORE_URL: process.env.HERMES_SIGNAL_STORE_URL || '',
      SIGNAL_IMPORTER_PYTHON: process.env.SIGNAL_IMPORTER_PYTHON || 'python3',
      SIGNAL_IMPORTER_MODULE: process.env.SIGNAL_IMPORTER_MODULE || '',
      SIGNAL_IMPORTER_CWD: process.env.SIGNAL_IMPORTER_CWD || '/Users/balen/git/freqtrade',
      SIGNAL_IMPORTER_REMOTE_HOST: process.env.SIGNAL_IMPORTER_REMOTE_HOST || '',
      SIGNAL_IMPORTER_REMOTE_CWD: process.env.SIGNAL_IMPORTER_REMOTE_CWD || '/root/freqtrade',
      SIGNAL_IMPORTER_SSH_IPV6: process.env.SIGNAL_IMPORTER_SSH_IPV6 || '0',
      SIGNAL_PAIR_WHITELIST: process.env.SIGNAL_PAIR_WHITELIST || '',
      SIGNAL_IMPORTER_ENABLED: process.env.SIGNAL_IMPORTER_ENABLED || '0',
      SIGNAL_IMPORTER_MAX_IN_FLIGHT: process.env.SIGNAL_IMPORTER_MAX_IN_FLIGHT || '1',
      HERMES_TRADER_CRON_ENABLED: process.env.HERMES_TRADER_CRON_ENABLED || '0',
      PRICE_MONITOR_ENABLED: process.env.PRICE_MONITOR_ENABLED || '1',
      TELEGRAM_PROXY_ENABLED: process.env.TELEGRAM_PROXY_ENABLED || '1',
      TELEGRAM_PROXY_HOST: process.env.TELEGRAM_PROXY_HOST || '127.0.0.1',
      TELEGRAM_PROXY_PORT: process.env.TELEGRAM_PROXY_PORT || '7897',
      TELEGRAM_PROXY_SOCKS_TYPE: process.env.TELEGRAM_PROXY_SOCKS_TYPE || '5',
      TELEGRAM_PROXY_TIMEOUT: process.env.TELEGRAM_PROXY_TIMEOUT || '10'
    },
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    error_file: '/Users/balen/.pm2/logs/telegram-watcher-error.log',
    out_file: '/Users/balen/.pm2/logs/telegram-watcher-out.log',
    merge_logs: true,
    // 守护进程配置
    autorestart: true,
    restart_delay: 3000,         // 崩溃后 3 秒重启
    max_restarts: 50,            // 允许更多次重启
    min_uptime: '10s',           // 运行 10 秒以上才算正常启动
    exp_backoff_restart_delay: 1000, // 指数退避重启（1s → 2s → 4s...最大 15s）
    kill_timeout: 5000,          // 优雅关闭超时
    listen_timeout: 10000,       // 启动超时
    // 日志轮转
    log_type: 'json',
    max_size: '10M',             // 单个日志文件最大 10MB
    retain: 5,                   // 保留 5 个轮转文件
  }]
};
