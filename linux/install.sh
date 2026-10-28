#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SOURCE/.." && pwd)"
DEST="${DS_LINUX_BALANCE_PET_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/ds-linux-balance-pet}"
APPLICATIONS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
AUTOSTART_ENABLED=0
UNINSTALL=0

for argument in "$@"; do
  case "$argument" in
    --autostart) AUTOSTART_ENABLED=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --help|-h)
      printf '用法: %s [--autostart] [--uninstall]\n' "$0"
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$argument" >&2
      exit 2
      ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  rm -rf "$DEST"
  rm -f "$APPLICATIONS/ds-linux-balance-pet.desktop"
  rm -f "$AUTOSTART/ds-linux-balance-pet.desktop"
  printf '已卸载 %s\n' "$DEST"
  printf '配置和余额记录保留在 %s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/ds-linux-balance-pet 和 ${XDG_STATE_HOME:-$HOME/.local/state}/ds-linux-balance-pet"
  exit 0
fi

if ! python3 - <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo
PY
then
  printf '缺少 GTK3/PyGObject。请先运行：\n' >&2
  printf '  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 fonts-noto-cjk\n' >&2
  exit 1
fi

mkdir -p "$DEST" "$APPLICATIONS"
rm -rf "$DEST/ds_pet" "$DEST/assets" "$DEST/tools"
cp -a "$SOURCE/ds_pet" "$DEST/ds_pet"
cp -a "$SOURCE/assets" "$DEST/assets"
mkdir -p "$DEST/tools"
cp "$REPO/tools/balance_report.py" "$DEST/tools/balance_report.py"
install -m 755 "$SOURCE/run.sh" "$DEST/run.sh"

cat > "$APPLICATIONS/ds-linux-balance-pet.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=余额桌宠
Comment=DeepSeek 余额桌宠
Exec=env GDK_BACKEND=x11 "$DEST/run.sh"
Icon=$DEST/assets/pet-base.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

if [ "$AUTOSTART_ENABLED" -eq 1 ]; then
  mkdir -p "$AUTOSTART"
  cp "$APPLICATIONS/ds-linux-balance-pet.desktop" "$AUTOSTART/ds-linux-balance-pet.desktop"
  printf '已启用登录自动启动。\n'
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS" >/dev/null 2>&1 || true
fi

printf '安装完成：%s\n' "$DEST"
printf '启动：%s/run.sh\n' "$DEST"
printf '设置密钥：%s/run.sh --set-key\n' "$DEST"
printf '临时缩放：%s/run.sh --scale 0.3\n' "$DEST"
printf '动画模式：%s/run.sh --mode compact  或  --mode classic\n' "$DEST"
printf '余额报告：python3 %s/tools/balance_report.py\n' "$DEST"
printf '桌面启动器：%s/ds-linux-balance-pet.desktop\n' "$APPLICATIONS"
printf '\n配置说明：\n'
printf '  配置文件：%s/config.json\n' "${XDG_CONFIG_HOME:-$HOME/.config}/ds-linux-balance-pet"
printf '  数据目录：%s/\n' "${XDG_STATE_HOME:-$HOME/.local/state}/ds-linux-balance-pet"
printf '  DEEPSEEK_API_KEY 环境变量优先于配置文件。\n'
printf '  常用字段：api_key / scale / animation_mode / low_threshold / refresh_seconds\n'
printf '  默认不开启开机自启；需要时运行本脚本时加 --autostart。\n'
