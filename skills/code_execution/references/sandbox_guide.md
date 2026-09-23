# دليل البيئة المعزولة

هذه المرجع يفصّل ضمانات `shell_execute` و `run_python`.

## الحدود المطبقة

- CPU 30s (`RLIMIT_CPU`)
- Memory 512 MB (`RLIMIT_AS`)
- Open files 64 (`RLIMIT_NOFILE`)
- Processes 32 (`RLIMIT_NPROC` على Linux)
- File size 10 MB (`RLIMIT_FSIZE`)
- Timeout ثانوي عبر `timeout` + `killpg` بعد N ثانية

## العزل

- `cwd` = `workspace/` دائماً — حتى لو كتب `cd / && rm -rf /`، يُنفذ كـ `bash -lc 'cd workspace && <command>'` داخل الحاوية
- `env` منقّى: يُحذف `AWS_*`, `OPENAI_*`, `GOOGLE_*`, `GEMINI_*`, `NVIDIA_*`, `SSH_AUTH_SOCK`, `GITHUB_TOKEN`
- `stdin` مغلق، لا `pty`
- الشبكة معطلة افتراضياً (`--network=none`) — لا `curl` إلا في وضع الحاوية مع موافقة

## الفلترة

قائمة الحظر قبل الموافقة:
- `rm -rf /`, `mkfs`, `dd`, `:(){:|:&};:`, `shutdown`, `reboot`
- `curl|sh`, `wget|bash`, `base64|bash`
- `nc`, `ncat`, `ssh`, `screen`, `tmux`, `pty`
- `chmod +s`, `chown`, `iptables`

حتى مع الفلتر، الموافقة البشرية مطلوبة لكل أمر.
