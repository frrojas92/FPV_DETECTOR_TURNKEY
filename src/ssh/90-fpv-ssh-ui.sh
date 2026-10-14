# Managed by FPV_SSH_UI_V1
# Sourced by /etc/profile at the start of an interactive SSH login shell.
# Keep non-interactive SSH/scp/sftp and local console sessions unchanged.
case "$-" in *i*) ;; *) return 0 ;; esac
[ -n "${SSH_CONNECTION:-}" ] || return 0
[ -t 1 ] || return 0
[ "${USER:-}" = 'cetel' ] || return 0
[ -z "${FPV_SSH_BANNER_SHOWN:-}" ] || return 0
export FPV_SSH_BANNER_SHOWN=1
if [ -x /usr/local/bin/fpv-banner ]; then
  /usr/local/bin/fpv-banner || true
fi
