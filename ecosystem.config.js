module.exports = {
  apps: [
    {
      name: "trump-speech-watch",
      cwd: "/home/nuonuo/app/trump-speech-watch",
      script: "main.py",
      interpreter: "/home/nuonuo/app/trump-speech-watch/.venv/bin/python",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      kill_timeout: 20000,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      out_file: "/home/nuonuo/app/trump-speech-watch/logs/pm2-out.log",
      error_file: "/home/nuonuo/app/trump-speech-watch/logs/pm2-error.log",
      merge_logs: false,
    },
  ],
};
