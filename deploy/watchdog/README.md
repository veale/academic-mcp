# Container watchdog

Docker's restart policy does not retry a container whose start fails while
the daemon is starting (for example after an automatic moby-engine upgrade).
`container-watchdog.sh` starts any of the named containers that is not running
and has a start error recorded in `.State.Error`. Containers stopped with
`docker stop` have no such error and are left alone.

Install as a user timer (needs `loginctl enable-linger $USER`, already set on asahi),
from this directory:

```sh
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m 755 container-watchdog.sh ~/.local/bin/
install -m 644 container-watchdog.service container-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now container-watchdog.timer
```

Check it with `journalctl --user -u container-watchdog`; pause it with
`systemctl --user stop container-watchdog.timer`.
