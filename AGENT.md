# ACCESS

- `CLI_host.py` is running on the target server, allowing you to send text commands and receive responses from this session.
- It has already been started on the target server with `python CLI_host.py --port 8333 --token 1234`.
- Public URL: `http://duynq.103.164.226.13.sslip.io`, add `:8333` at the end if necessary.
- To upload or download files, use `scp` with `duyn@103.164.226.13`.
- If the public URL, token, or `scp` connection does not work, stop immediately and report the issue to me.


# WORKFLOWS & NOTES

- Short commands: HTTP `POST /run` (raw body = command) or `GET /run?cmd=…`, token as `?token=`, `X-Token`, Bearer, or Basic. Response is full stdout+stderr when the process **exits**.
- Live output: WebSocket `ws://<host>:<port>/?token=<token>`. Send one text frame = command. Stdout/stderr stream as text frames; the socket closes when the command ends. Use this for pip, server boot, anything that prints over time.
- Files: `scp` (upload and download). Do not base64 through `/run`.
- Long-running daemons: `setsid cmd >/tmp/cmd.log 2>&1 </dev/null &` so `/run` can return. Watch the log over WS or a later `/run`.
- Notes: 
+ Each call is a new shell.
+ cwd is wherever `CLI_host.py` was started.
+ TCP-timeout -> retry once, then check if the work already started. 
+ From Windows, write the command to a file and `curl.exe --data-binary @file` (PowerShell quoting will eat nested quotes).


# DETAILS 
- Read the doc at [link](https://github.com/NnQqDd/remote-helpers/blob/main/README.md#cli_hostpy).
- Read the source code at [link](https://github.com/NnQqDd/remote-helpers/blob/main/CLI_host.py)
- If you can't access this links, stop immediately and report the issue to me.