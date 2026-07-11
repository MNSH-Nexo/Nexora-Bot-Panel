#!/usr/bin/env bash
# ============================================================
#  Nexora VPN Bot — Installer v1.8 (Hybrid Stable)
#  Built by MNSH-Nexo · github.com/MNSH-Nexo
#
#  Usage:  sudo bash install.sh
#
#  Supported OS:
#    Ubuntu 20.04 / 22.04 / 24.04
#    Debian 10 / 11 / 12
#    CentOS / RHEL / AlmaLinux / Rocky Linux 8 / 9
# ============================================================
set -euo pipefail

# ── Colors & UI ─────────────────────────────────────────────
RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BLUE='\033[0;34m';   BOLD='\033[1m'
DIM='\033[2m';     RESET='\033[0m'

_banner() {
  clear
  echo -e "${CYAN}${BOLD}"
  echo "  ╔══════════════════════════════════════════════════════╗"
  echo "  ║       NEXORA VPN BOT  —  INSTALLER  v1.8            ║"
  echo "  ║       Hybrid Stable  ·  Built by MNSH-Nexo          ║"
  echo "  ╚══════════════════════════════════════════════════════╝"
  echo -e "${RESET}"
}

_header()  { echo -e "\n${BLUE}${BOLD}━━━  $*  ━━━${RESET}"; }
_step()    { echo -e "\n${CYAN}${BOLD}▶  $*${RESET}"; }
_ok()      { echo -e "   ${GREEN}✔${RESET}  $*"; }
_warn()    { echo -e "   ${YELLOW}⚠${RESET}  $*"; }
_err()     { echo -e "   ${RED}✘  ERROR: $*${RESET}" >&2; }
_info()    { echo -e "   ${DIM}ℹ  $*${RESET}"; }
_line()    { echo -e "${DIM}  ──────────────────────────────────────────────────────${RESET}"; }
_ask()     { read -rp "$(echo -e "   ${BOLD}?${RESET}  $1: ")" "$2"; }
_ask_s()   { read -rsp "$(echo -e "   ${BOLD}?${RESET}  $1: ")" "$2"; echo ""; }
_progress(){ echo -ne "   ${DIM}$*${RESET}\r"; }

# ── Sanity checks ────────────────────────────────────────────
[[ $EUID -ne 0 ]] && { _err "Run as root: sudo bash install.sh"; exit 1; }
[[ -z "${BASH_VERSION:-}" ]] && { _err "bash required"; exit 1; }

# ── Script dir (nexora-panel.zip lives here) ─────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ══════════════════════════════════════════════════════════════
#  OS & package manager
# ══════════════════════════════════════════════════════════════
OS_ID=""; OS_LIKE=""; PKG_MANAGER=""

_detect_os() {
  [[ -f /etc/os-release ]] && { source /etc/os-release; OS_ID="${ID:-}"; OS_LIKE="${ID_LIKE:-}"; }
  if   command -v apt-get &>/dev/null; then PKG_MANAGER="apt"
  elif command -v dnf     &>/dev/null; then PKG_MANAGER="dnf"
  elif command -v yum     &>/dev/null; then PKG_MANAGER="yum"
  else                                      PKG_MANAGER="unknown"
  fi
}

# ── Retry wrapper ─────────────────────────────────────────────
_retry() {
  local n="$1" delay="$2"; shift 2
  local i=1
  until "$@"; do
    (( i >= n )) && { _err "Failed after $n attempts: $*"; return 1; }
    _warn "Attempt $i/$n failed — retrying in ${delay}s..."
    sleep "$delay"; (( i++ ))
  done
}

# ── DNS helpers ───────────────────────────────────────────────
_DNS_FIXED=false
_fix_dns() {
  [[ "$_DNS_FIXED" == "true" ]] && return
  _warn "DNS resolution failed — applying fallback DNS..."
  local rc="/etc/resolv.conf"
  command -v chattr &>/dev/null && chattr -i "$rc" 2>/dev/null || true
  { grep "^nameserver" "$rc" 2>/dev/null || true
    echo "nameserver 8.8.8.8"; echo "nameserver 1.1.1.1"; echo "nameserver 9.9.9.9"
  } | awk '!seen[$0]++' > /tmp/_resolv.conf && cp /tmp/_resolv.conf "$rc"
  _DNS_FIXED=true; sleep 1
}
_ensure_dns() {
  local h="${1:-github.com}"
  getent hosts "$h" &>/dev/null || host "$h" &>/dev/null || \
  nslookup "$h"    &>/dev/null || curl -sfI "https://$h" -o /dev/null --max-time 5 &>/dev/null && return 0
  _fix_dns
  sleep 2
  getent hosts "$h" &>/dev/null || _warn "Cannot resolve $h — continuing anyway"
}

# ══════════════════════════════════════════════════════════════
#  Docker helpers
# ══════════════════════════════════════════════════════════════
_install_docker_apt() {
  _ensure_dns "download.docker.com"
  _info "Waiting for apt lock..."
  local w=0
  while fuser /var/lib/dpkg/lock-frontend &>/dev/null 2>&1; do
    sleep 2; (( w+=2 ))
    if (( w >= 60 )); then
      rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock
      dpkg --configure -a 2>/dev/null || true; break
    fi
  done
  _retry 3 5 apt-get update -qq
  _retry 3 5 apt-get install -y -qq ca-certificates curl gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  local distro="ubuntu"
  echo "${OS_ID} ${OS_LIKE}" | grep -qi "debian" && \
    ! echo "${OS_ID} ${OS_LIKE}" | grep -qi "ubuntu" && distro="debian"
  local codename
  codename="$(lsb_release -cs 2>/dev/null || true)"
  [[ -z "$codename" ]] && { source /etc/os-release 2>/dev/null || true; codename="${VERSION_CODENAME:-}"; }
  [[ -z "$codename" ]] && { _err "Cannot detect OS codename"; return 1; }
  _retry 3 5 bash -c "curl -fsSL https://download.docker.com/linux/${distro}/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg"
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${distro} ${codename} stable" > /etc/apt/sources.list.d/docker.list
  _retry 3 5  apt-get update -qq
  _retry 3 10 apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
}

_install_docker_rpm() {
  _ensure_dns "download.docker.com"
  if command -v dnf &>/dev/null; then
    dnf remove -y podman buildah 2>/dev/null || true
    _retry 3 5  dnf install -y dnf-plugins-core
    _retry 3 5  dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    _retry 3 10 dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  else
    yum remove -y podman buildah 2>/dev/null || true
    _retry 3 5  yum install -y yum-utils
    _retry 3 5  yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    _retry 3 10 yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
}

_install_docker() {
  case "$PKG_MANAGER" in
    apt)        _install_docker_apt ;;
    dnf|yum)    _install_docker_rpm ;;
    *)          _retry 3 10 bash -c "curl -fsSL https://get.docker.com | sh" ;;
  esac
  command -v systemctl &>/dev/null && systemctl enable docker --now 2>/dev/null || true
  command -v docker &>/dev/null || { _err "Docker install failed"; exit 1; }
  _ok "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
}

_ensure_compose() {
  docker compose version &>/dev/null 2>&1 && return 0
  _warn "docker compose (v2) missing — installing..."
  case "$PKG_MANAGER" in
    apt)     _retry 3 5 apt-get install -y -qq docker-compose-plugin ;;
    dnf)     _retry 3 5 dnf install -y docker-compose-plugin ;;
    yum)     _retry 3 5 yum install -y docker-compose-plugin ;;
    *)
      local v; v=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
        | grep '"tag_name"' | cut -d'"' -f4 2>/dev/null || echo "v2.27.0")
      local dest="/usr/local/lib/docker/cli-plugins"; mkdir -p "$dest"
      _retry 3 10 curl -fsSL \
        "https://github.com/docker/compose/releases/download/${v}/docker-compose-$(uname -s)-$(uname -m)" \
        -o "${dest}/docker-compose"
      chmod +x "${dest}/docker-compose"
      ;;
  esac
  docker compose version &>/dev/null 2>&1 || { _err "docker compose still missing"; exit 1; }
}

# ══════════════════════════════════════════════════════════════
#  Docker build helper — از نسخه B (fast + psycopg2-binary)
# ══════════════════════════════════════════════════════════════
_docker_build_bot() {
  local _img="nexora-vpn-bot:latest"

  # ── روش ۱: بدون apt-get — سریع، psycopg2-binary ──────────────────
  _info "Method 1/2: Fast build (no apt-get, pip-only)..."
  local _fast_df="/tmp/Dockerfile.bot.fast"
  cat > "$_fast_df" <<'FASTEOF'
FROM python:3.11-slim
LABEL maintainer="vpn-bot" description="Telegram VPN Bot"
WORKDIR /app
COPY requirements.txt .
# جایگزینی psycopg2 با psycopg2-binary (pre-compiled، نیازی به gcc نیست)
RUN sed 's/psycopg2[^-]/psycopg2-binary/g' requirements.txt > /tmp/req_patched.txt \
    && pip install --no-cache-dir -r /tmp/req_patched.txt
COPY . .
RUN useradd -m -u 1001 botuser \
    && mkdir -p /app/logs /app/data \
    && chown -R botuser:botuser /app
USER botuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"
CMD ["python", "main.py"]
FASTEOF

  if timeout 120 docker build \
      --network=host \
      --no-cache \
      -f "$_fast_df" \
      -t "$_img" . 2>&1; then
    _ok "Image built ✓ (fast/pip-only method)"
    _patch_compose_for_prebuilt_image "$_img"
    rm -f "$_fast_df"
    return 0
  fi
  rm -f "$_fast_df"
  _warn "Fast build failed — trying full build with apt-get..."

  # ── روش ۲: build کامل با apt-get + timeout 90s ──────────────────
  _info "Method 2/2: Full build --network=host (apt timeout 90s)..."
  local _full_df="/tmp/Dockerfile.bot.full"
  cat > "$_full_df" <<'FULLEOF'
FROM python:3.11-slim
LABEL maintainer="vpn-bot" description="Telegram VPN Bot"
WORKDIR /app
RUN echo 'Acquire::http::Timeout "90"; Acquire::https::Timeout "90"; Acquire::Retries "2";' \
      > /etc/apt/apt.conf.d/99-timeout \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 1001 botuser \
    && mkdir -p /app/logs /app/data \
    && chown -R botuser:botuser /app
USER botuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"
CMD ["python", "main.py"]
FULLEOF

  if timeout 300 docker build \
      --network=host \
      --no-cache \
      -f "$_full_df" \
      -t "$_img" . 2>&1; then
    _ok "Image built ✓ (full build with apt)"
    _patch_compose_for_prebuilt_image "$_img"
    rm -f "$_full_df"
    return 0
  fi
  rm -f "$_full_df"

  _err "Both build methods failed."
  _err "Check PyPI access: docker run --rm python:3.11-slim pip install aiogram"
  return 1
}

# patch docker-compose.yml برای استفاده از image از پیش build شده
_patch_compose_for_prebuilt_image() {
  local _img="$1"
  _info "Patching docker-compose.yml to use pre-built image: $_img..."
  python3 - "$_img" <<'PYEOF'
import sys, re

img = sys.argv[1]

with open("docker-compose.yml", "r") as f:
    lines = f.readlines()

out = []
i = 0
while i < len(lines):
    line = lines[i]
    if re.match(r'^  bot:\s*$', line):
        out.append(line)
        i += 1
        skip_build = False
        image_written = False
        while i < len(lines):
            l = lines[i]
            if re.match(r'^  \w', l) and not re.match(r'^  #', l):
                break
            if re.match(r'^\s{4}build:\s*$', l):
                skip_build = True
                i += 1
                continue
            if skip_build and re.match(r'^\s{6,}', l):
                i += 1
                continue
            skip_build = False
            if re.match(r'^\s{4}image:\s*', l):
                out.append(f"    image: {img}\n")
                image_written = True
                i += 1
                continue
            out.append(l)
            i += 1
        if not image_written:
            out.append(f"    image: {img}\n")
    else:
        out.append(line)
        i += 1

with open("docker-compose.yml", "w") as f:
    f.writelines(out)

print(f"docker-compose.yml patched → build: removed, image: {img}")
PYEOF
  _ok "docker-compose.yml updated"
}

# ══════════════════════════════════════════════════════════════
#  Node / pnpm helpers
# ══════════════════════════════════════════════════════════════
PANEL_INSTALL_DIR="/opt/nexora-panel"
PANEL_RELEASE_URL="${PANEL_RELEASE_URL:-https://github.com/MNSH-Nexo/Nexora-Bot/releases/latest/download/nexora-panel.zip}"

_install_nodejs() {
  if command -v node &>/dev/null; then
    local v; v=$(node --version 2>/dev/null | tr -d 'v' | cut -d. -f1)
    (( v >= 22 )) && { _ok "Node.js: $(node --version)"; return 0; }
    _warn "Node.js $(node --version) is too old — upgrading to v22..."
  else
    _info "Installing Node.js v22..."
  fi
  _ensure_dns "deb.nodesource.com"
  case "$PKG_MANAGER" in
    apt)
      _retry 3 5  bash -c "curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1"
      _retry 3 10 apt-get install -y -qq nodejs
      ;;
    dnf)
      _retry 3 5  bash -c "curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >/dev/null 2>&1"
      _retry 3 10 dnf install -y nodejs
      ;;
    yum)
      _retry 3 5  bash -c "curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >/dev/null 2>&1"
      _retry 3 10 yum install -y nodejs
      ;;
    *)
      export NVM_DIR="$HOME/.nvm"
      _retry 3 10 bash -c "curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash"
      [[ -f "$NVM_DIR/nvm.sh" ]] && source "$NVM_DIR/nvm.sh"
      nvm install 22 && nvm use 22 && nvm alias default 22
      ;;
  esac
  command -v node &>/dev/null || { _err "Node.js install failed"; return 1; }
  _ok "Node.js: $(node --version)"
}

_install_pnpm() {
  local _target="/usr/local/bin/pnpm"
  local _ver="9.15.9"
  local _arch; _arch=$(uname -m)

  _pnpm_works() {
    local _b="$1"
    [[ -x "$_b" ]] || return 1
    local _v; _v=$("$_b" --version 2>/dev/null | grep -oE '^[0-9]+' || echo "0")
    (( _v >= 9 ))
  }

  if _pnpm_works "$_target"; then
    _ok "pnpm: $("$_target" --version)"; return 0
  fi

  # ── Fix: pnpm-workspace.yaml بدون packages field ────────────
  for _ws in /root/pnpm-workspace.yaml /home/*/pnpm-workspace.yaml; do
    [[ -f "$_ws" ]] || continue
    if ! grep -q "^packages:" "$_ws" 2>/dev/null; then
      _info "Fixing broken pnpm-workspace.yaml: $_ws"
      { echo "packages:"; echo "  - ."; echo ""; cat "$_ws"; } > "${_ws}.tmp" \
        && mv "${_ws}.tmp" "$_ws"
    fi
  done

  # ── پاک‌سازی stubs از همه مسیرها ─────────────────────────
  _info "Cleaning all pnpm stubs..."
  for _p in /usr/local/bin/pnpm /usr/bin/pnpm /bin/pnpm \
             "$HOME/.local/bin/pnpm" "$HOME/.local/share/pnpm/pnpm" \
             "/root/.local/share/pnpm/pnpm"; do
    rm -f "$_p" 2>/dev/null || true
  done
  hash -r 2>/dev/null || true

  # ── روش ۱: از global node_modules که از قبل نصب شده ────────
  _info "Method 1/5: Use existing global pnpm.cjs..."
  for _cjs in \
      /usr/local/lib/node_modules/pnpm/bin/pnpm.cjs \
      /usr/lib/node_modules/pnpm/bin/pnpm.cjs \
      "$(npm root -g 2>/dev/null)/pnpm/bin/pnpm.cjs"; do
    if [[ -f "$_cjs" ]] && node "$_cjs" --version &>/dev/null 2>&1; then
      printf '#!/bin/sh\nexec node "%s" "$@"\n' "$_cjs" > "$_target"
      chmod +x "$_target"
      _pnpm_works "$_target" && { _ok "pnpm: $("$_target" --version)"; return 0; }
    fi
  done
  rm -f "$_target" 2>/dev/null || true

  # ── روش ۲: jsDelivr CDN → pnpm.cjs ──────────────────────────
  _info "Method 2/5: jsDelivr CDN pnpm.cjs..."
  local _cjs_file="/usr/local/lib/pnpm-${_ver}.cjs"
  if curl -fsSL --retry 3 --max-time 60 \
      "https://cdn.jsdelivr.net/npm/pnpm@${_ver}/dist/pnpm.cjs" \
      -o "$_cjs_file" 2>/dev/null \
      && [[ -s "$_cjs_file" ]] \
      && node "$_cjs_file" --version &>/dev/null 2>&1; then
    printf '#!/bin/sh\nexec node "%s" "$@"\n' "$_cjs_file" > "$_target"
    chmod +x "$_target"
    _pnpm_works "$_target" && { _ok "pnpm: $("$_target" --version)"; return 0; }
  fi
  rm -f "$_target" 2>/dev/null || true

  # ── روش ۳: GitHub binary مستقیم ─────────────────────────
  _info "Method 3/5: GitHub binary download..."
  local _base="https://github.com/pnpm/pnpm/releases/download/v${_ver}"
  local _url; case "$_arch" in
    x86_64)  _url="${_base}/pnpm-linux-x64" ;;
    aarch64) _url="${_base}/pnpm-linux-arm64" ;;
    *)       _url="${_base}/pnpm-linux-x64" ;;
  esac
  if curl -fsSL --retry 2 --max-time 30 "$_url" -o "$_target" 2>/dev/null; then
    chmod +x "$_target"
    _pnpm_works "$_target" && { _ok "pnpm: $("$_target" --version)"; return 0; }
  fi
  rm -f "$_target" 2>/dev/null || true

  # ── روش ۴: jsdelivr CDN → pnpm.cjs (بار دوم) ────────────
  _info "Method 4/5: jsDelivr CDN retry..."
  local _cjs_cdn="https://cdn.jsdelivr.net/npm/pnpm@${_ver}/dist/pnpm.cjs"
  if curl -fsSL --retry 2 --max-time 30 "$_cjs_cdn" -o "$_cjs_file" 2>/dev/null \
      && [[ -s "$_cjs_file" ]] && node "$_cjs_file" --version &>/dev/null 2>&1; then
    printf '#!/bin/sh\nexec node "%s" "$@"\n' "$_cjs_file" > "$_target"
    chmod +x "$_target"
    _pnpm_works "$_target" && { _ok "pnpm: $("$_target" --version)"; return 0; }
  fi
  rm -f "$_target" 2>/dev/null || true

  # ── روش ۵: corepack ──────────────────────────────────────
  _info "Method 5/5: corepack..."
  corepack enable 2>/dev/null || true
  corepack prepare "pnpm@${_ver}" --activate 2>/dev/null || true
  hash -r 2>/dev/null || true
  for _c in \
      "$HOME/.local/share/pnpm/pnpm" \
      "/root/.local/share/pnpm/pnpm" \
      "$(npm root -g 2>/dev/null)/../bin/pnpm"; do
    if [[ -x "$_c" ]] && "$_c" --version &>/dev/null 2>&1; then
      cp "$_c" "$_target" 2>/dev/null || \
        { printf '#!/bin/sh\nexec "%s" "$@"\n' "$_c" > "$_target"; chmod +x "$_target"; }
      _pnpm_works "$_target" && { _ok "pnpm (corepack): $("$_target" --version)"; return 0; }
    fi
  done

  _err "All 5 methods failed."
  _err "Manual fix: curl -fsSL https://cdn.jsdelivr.net/npm/pnpm@${_ver}/dist/pnpm.cjs -o /usr/local/lib/pnpm.cjs && printf '#!/bin/sh\\nexec node /usr/local/lib/pnpm.cjs \"\$@\"\\n' > /usr/local/bin/pnpm && chmod +x /usr/local/bin/pnpm"
  return 1
}

_ensure_unzip() {
  command -v unzip &>/dev/null && return 0
  _info "Installing unzip..."
  case "$PKG_MANAGER" in
    apt)     apt-get install -y -qq unzip ;;
    dnf|yum) ${PKG_MANAGER} install -y unzip ;;
  esac
}

# ══════════════════════════════════════════════════════════════
#  xui.ts patcher — v3
#  جایگزین کردن lib/xui.ts با نسخه‌ای که:
#  - redirect: manual (جلوگیری از گم شدن Set-Cookie)
#  - form-encoded login fallback (برای نسخه‌های قدیمی 3X-UI)
#  - session TTL 30 دقیقه (re-login خودکار)
#  - timeout 20 ثانیه (به جای 15)
# ══════════════════════════════════════════════════════════════
_patch_xui_ts() {
  local _target="$1"
  cat > "$_target" <<'XUIEOF'
/**
 * lib/xui.ts — v3 (VPS-patched by installer)
 * تغییرات: redirect:manual + form-encoded fallback + session TTL + timeout 20s
 */

function getPanelConfig() {
  const url      = (process.env.PANEL_URL      ?? "").trim().replace(/\/$/, "");
  const username = (process.env.PANEL_USERNAME ?? "admin").trim();
  const password = (process.env.PANEL_PASSWORD ?? "").trim();
  return { url, username, password };
}

function withTlsBypass<T>(fn: () => Promise<T>): Promise<T> {
  const prev = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
  return fn().finally(() => {
    if (prev === undefined) delete process.env.NODE_TLS_REJECT_UNAUTHORIZED;
    else process.env.NODE_TLS_REJECT_UNAUTHORIZED = prev;
  });
}

function extractCookies(res: Response): string {
  if (typeof res.headers.getSetCookie === "function") {
    const cs = res.headers.getSetCookie();
    if (cs.length > 0) return cs.map((c: string) => c.split(";")[0]).join("; ");
  }
  const s = res.headers.get("set-cookie") ?? "";
  return s ? s.split(";")[0] : "";
}

function mergeCookies(base: string, over: string): string {
  if (!base) return over;
  if (!over)  return base;
  if (base === over) return base;
  try {
    const parse = (s: string) => Object.fromEntries(
      s.split(";").map(p => { const [k,...v] = p.trim().split("="); return [k.trim(), v.join("=")] as [string,string]; })
    );
    const merged = { ...parse(base), ...parse(over) };
    return Object.entries(merged).filter(([k]) => k).map(([k,v]) => v ? `${k}=${v}` : k).join("; ");
  } catch { return over || base; }
}

function extractCsrfToken(html: string): string {
  const m = html.match(/<meta\s+name=["']csrf-token["']\s+content=["']([^"']+)["']/i)
         ?? html.match(/<meta\s+content=["']([^"']+)["']\s+name=["']csrf-token["']/i)
         ?? html.match(/csrfToken['":\s]+["']([a-zA-Z0-9_-]{20,})["']/i);
  return m?.[1] ?? "";
}

let _sessionCookie = "";
let _csrfToken     = "";
let _loggedIn      = false;
let _lastLoginAt   = 0;

function resetSession() {
  _sessionCookie = ""; _csrfToken = ""; _loggedIn = false; _lastLoginAt = 0;
}

async function fetchCsrfToken(panelUrl: string): Promise<{ cookie: string; csrf: string }> {
  return withTlsBypass(async () => {
    try {
      const res = await fetch(`${panelUrl}/`, {
        method: "GET", redirect: "manual",
        headers: { Accept: "text/html,*/*", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9" },
        signal: AbortSignal.timeout(20_000),
      });
      const initCookie = extractCookies(res);
      if (res.status >= 300 && res.status < 400) {
        const loc = res.headers.get("location") ?? "";
        if (loc) {
          try {
            const res2 = await fetch(loc.startsWith("http") ? loc : `${panelUrl}${loc}`, {
              headers: { Accept: "text/html,*/*", "User-Agent": "Mozilla/5.0", ...(initCookie ? { Cookie: initCookie } : {}) },
              signal: AbortSignal.timeout(20_000),
            });
            const html2 = await res2.text();
            return { cookie: mergeCookies(initCookie, extractCookies(res2)), csrf: extractCsrfToken(html2) };
          } catch { /* ignore */ }
        }
      }
      const html = await res.text();
      return { cookie: initCookie, csrf: extractCsrfToken(html) };
    } catch { return { cookie: "", csrf: "" }; }
  });
}

async function performLogin(panelUrl: string, username: string, password: string, initialCookie: string, csrfToken: string): Promise<string | null> {
  return withTlsBypass(async () => {
    const base: Record<string,string> = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      "Origin": panelUrl, "Referer": `${panelUrl}/`,
      ...(initialCookie ? { Cookie: initialCookie } : {}),
      ...(csrfToken     ? { "X-CSRF-Token": csrfToken } : {}),
    };

    // روش ۱: JSON
    const tryJson = async (): Promise<string | null> => {
      const res = await fetch(`${panelUrl}/login`, {
        method: "POST", redirect: "manual",
        headers: { ...base, "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ username, password }),
        signal: AbortSignal.timeout(20_000),
      });
      const sc = extractCookies(res);
      if (sc) return mergeCookies(initialCookie, sc);
      if ((res.status >= 300 && res.status < 400) || res.ok) {
        const loc = res.headers.get("location") ?? "";
        if (loc) {
          try {
            const r2 = await fetch(loc.startsWith("http") ? loc : `${panelUrl}${loc}`, { redirect:"manual", headers:{...base, Cookie: initialCookie}, signal: AbortSignal.timeout(10_000) });
            const sc2 = extractCookies(r2);
            if (sc2) return mergeCookies(initialCookie, sc2);
          } catch { /* ignore */ }
        }
        try {
          const body = await res.text();
          if (body.includes('"success":true')) return initialCookie || null;
        } catch { /* ignore */ }
      }
      return null;
    };

    // روش ۲: form-encoded
    const tryForm = async (): Promise<string | null> => {
      const res = await fetch(`${panelUrl}/login`, {
        method: "POST", redirect: "manual",
        headers: { ...base, "Content-Type": "application/x-www-form-urlencoded", Accept: "text/html,*/*" },
        body: new URLSearchParams({ username, password }).toString(),
        signal: AbortSignal.timeout(20_000),
      });
      const sc = extractCookies(res);
      return sc ? mergeCookies(initialCookie, sc) : null;
    };

    const r1 = await tryJson();
    if (r1) return r1;
    return await tryForm();
  });
}

export async function xuiLogin(): Promise<string | null> {
  const { url, username, password } = getPanelConfig();
  if (!url || !password) return null;
  try {
    const { cookie: ic, csrf } = await fetchCsrfToken(url);
    const sc = await performLogin(url, username, password, ic, csrf);
    if (!sc) return null;
    _sessionCookie = sc; _csrfToken = csrf; _loggedIn = true; _lastLoginAt = Date.now();
    return sc;
  } catch { resetSession(); return null; }
}

async function xuiFetchWithSession(url: string, init: RequestInit = {}): Promise<Response | null> {
  if (!_loggedIn || (Date.now() - _lastLoginAt) > 30 * 60 * 1000) {
    if (!await xuiLogin()) return null;
  }
  return withTlsBypass(async () => {
    const { url: pu } = getPanelConfig();
    const h: Record<string,string> = {
      ...(init.headers as Record<string,string> ?? {}),
      Cookie: _sessionCookie, "User-Agent": "Mozilla/5.0", Referer: `${pu}/`,
      ..._csrfToken ? { "X-CSRF-Token": _csrfToken } : {},
    };
    const res = await fetch(url, { ...init, headers: h, signal: init.signal ?? AbortSignal.timeout(20_000) });
    if (res.status === 401 || res.status === 403) {
      resetSession();
      if (!await xuiLogin()) return null;
      return withTlsBypass(() => fetch(url, { ...init, headers: { ...(init.headers as Record<string,string> ?? {}), Cookie: _sessionCookie, "X-CSRF-Token": _csrfToken, "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(20_000) }));
    }
    return res;
  });
}

export async function xuiGetInbounds(): Promise<Array<Record<string, unknown>> | null> {
  const { url } = getPanelConfig();
  if (!url) return null;
  try {
    const res = await xuiFetchWithSession(`${url}/panel/api/inbounds/list`, { headers: { Accept: "application/json" } });
    if (!res || !res.ok) return null;
    const data = await res.json() as { success: boolean; obj?: unknown[] };
    if (!data.success) return null;
    return (data.obj as Array<Record<string, unknown>>) ?? null;
  } catch { return null; }
}

export type XuiServerStatus = {
  cpu: number; cpuCores: number;
  mem: { used: number; total: number }; swap: { used: number; total: number }; disk: { used: number; total: number };
  uptime: number; loads: number[]; xrayState: string; xrayVersion: string; panelVersion: string;
  netIO: { up: number; down: number }; tcpCount: number; udpCount: number;
};

export async function xuiGetServerStatus(): Promise<XuiServerStatus | null> {
  const { url } = getPanelConfig();
  if (!url) return null;
  try {
    const res = await xuiFetchWithSession(`${url}/panel/api/server/status`, { headers: { Accept: "application/json" } });
    if (!res || !res.ok) return null;
    const data = await res.json() as { success: boolean; obj?: Record<string, unknown> };
    if (!data.success || !data.obj) return null;
    const o = data.obj;
    const mem  = o.mem  as { current: number; total: number } | undefined;
    const swap = o.swap as { current: number; total: number } | undefined;
    const disk = o.disk as { current: number; total: number } | undefined;
    const xray = o.xray as { state: string; version: string } | undefined;
    const netIO = o.netIO as { up: number; down: number } | undefined;
    return {
      cpu: typeof o.cpu === "number" ? Math.round(o.cpu) : 0,
      cpuCores: typeof o.cpuCores === "number" ? o.cpuCores : 1,
      mem:  { used: mem?.current  ?? 0, total: mem?.total  ?? 0 },
      swap: { used: swap?.current ?? 0, total: swap?.total ?? 0 },
      disk: { used: disk?.current ?? 0, total: disk?.total ?? 0 },
      uptime:       typeof o.uptime === "number" ? o.uptime : 0,
      loads:        Array.isArray(o.loads) ? (o.loads as number[]) : [0,0,0],
      xrayState:    xray?.state   ?? "unknown",
      xrayVersion:  xray?.version ?? "",
      panelVersion: typeof o.panelVersion === "string" ? o.panelVersion : "",
      netIO:    { up: netIO?.up ?? 0, down: netIO?.down ?? 0 },
      tcpCount: typeof o.tcpCount === "number" ? o.tcpCount : 0,
      udpCount: typeof o.udpCount === "number" ? o.udpCount : 0,
    };
  } catch { return null; }
}

export async function xuiPing(): Promise<{ reachable: boolean; loginOk: boolean; csrfFound: boolean; error?: string }> {
  const { url, password } = getPanelConfig();
  if (!url)      return { reachable: false, loginOk: false, csrfFound: false, error: "PANEL_URL not set" };
  if (!password) return { reachable: false, loginOk: false, csrfFound: false, error: "PANEL_PASSWORD not set" };
  try {
    const { url: u, username, password: pass } = getPanelConfig();
    const { cookie: ic, csrf } = await fetchCsrfToken(u);
    const sc = await performLogin(u, username, pass, ic, csrf);
    if (sc) { _sessionCookie = sc; _csrfToken = csrf; _loggedIn = true; _lastLoginAt = Date.now(); }
    return { reachable: true, loginOk: !!sc, csrfFound: !!csrf };
  } catch (e: unknown) {
    return { reachable: false, loginOk: false, csrfFound: false, error: e instanceof Error ? e.message : String(e) };
  }
}

export function getPanelStatus(): { configured: boolean; url: string; username: string } {
  const { url, username, password } = getPanelConfig();
  return { configured: !!(url && password), url, username };
}

export function clearXuiSession(): void { resetSession(); }

export interface XuiNewClient { email: string; subId: string; subLink: string; inboundId: number; }

export async function xuiAddClient(inboundId: number, email: string, trafficGb = 0, expireDays = 30, limitIp = 0, tgId = 0): Promise<XuiNewClient | null> {
  const { url } = getPanelConfig();
  if (!url) return null;
  const subId = Array.from(crypto.getRandomValues(new Uint8Array(8))).map(b => b.toString(16).padStart(2,"0")).join("");
  const totalBytes = trafficGb  > 0 ? trafficGb  * 1024 * 1024 * 1024 : 0;
  const expiryTs   = expireDays > 0 ? Date.now() + expireDays * 86400 * 1000 : 0;
  const payload = { client: { email, totalGB: totalBytes, expiryTime: expiryTs, tgId, limitIp, enable: true, subId, reset: 0 }, inboundIds: [inboundId] };
  const res = await xuiFetchWithSession(`${url}/panel/api/clients/add`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!res || !res.ok) return null;
  const data = await res.json() as { success?: boolean };
  if (!data.success) return null;
  const subBase = url.replace(/\/[a-zA-Z0-9_-]{10,}$/, "");
  return { email, subId, subLink: `${subBase}/sub/${subId}`, inboundId };
}
XUIEOF
}

# ══════════════════════════════════════════════════════════════
#  botdb.ts patcher
#  جایگزینی static import با require() برای جلوگیری از module crash
# ══════════════════════════════════════════════════════════════
_patch_botdb_ts() {
  local _target="$1"
  cat > "$_target" <<'BOTDBEOF'
/**
 * lib/botdb.ts — v2 (VPS-patched by installer)
 * تغییر: require() به جای static import — اگه better-sqlite3 load نشه،
 * فقط getBotDb null برمی‌گرده (و error لاگ می‌شه) به جای کرش کل module.
 */
import fs from "fs";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _Sqlite: any = null;
let _sqliteErr: string | null = null;

function getSqlite() {
  if (_Sqlite !== null) return _Sqlite;
  if (_sqliteErr !== null) return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    _Sqlite = require("better-sqlite3");
    return _Sqlite;
  } catch (e) {
    _sqliteErr = String(e);
    console.error("[botdb] better-sqlite3 failed to load:", _sqliteErr);
    return null;
  }
}
getSqlite(); // warm-up at module load

function getDbCandidates(): string[] {
  const candidates: string[] = [];
  const envPath = process.env.BOT_DB_PATH?.trim();
  if (envPath) candidates.push(envPath);
  candidates.push("/var/lib/docker/volumes/nexora_bot_data/_data/bot_data.db");
  candidates.push("/opt/nexora-bot/data/bot_data.db");
  candidates.push("/opt/nexora-bot/bot/bot_data.db");
  return candidates;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function getBotDb(readonly = true): any | null {
  const Sqlite = getSqlite();
  if (!Sqlite) {
    console.error("[botdb] sqlite not available:", _sqliteErr);
    return null;
  }
  for (const p of getDbCandidates()) {
    if (!fs.existsSync(p)) continue;
    try {
      return new Sqlite(p, { readonly, fileMustExist: true });
    } catch (e) {
      console.error(`[botdb] open failed (${p}):`, String(e));
    }
  }
  console.error("[botdb] no DB found. candidates:", getDbCandidates());
  return null;
}

export function getBotDbPath(): string | null {
  for (const p of getDbCandidates()) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function getDbDiagnostics(): Record<string, any> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const result: Record<string, any> = {};
  for (const p of getDbCandidates()) result[p] = fs.existsSync(p);
  result["__sqlite_loaded"] = _Sqlite !== null;
  result["__sqlite_error"]  = _sqliteErr ?? "none";
  return result;
}

export function requireAuth(req: Request): boolean {
  const cookie = req.headers.get("cookie") ?? "";
  if (!cookie.includes("nexora_session=")) return false;
  const token = cookie.split("nexora_session=")[1]?.split(";")[0]?.trim() ?? "";
  if (token.length < 20) return false;
  try {
    const decoded = Buffer.from(token, "base64").toString("utf8");
    return decoded.split(":").length >= 3;
  } catch { return false; }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function getSetting(db: any, key: string, def = ""): string {
  try {
    const row = db.prepare("SELECT value FROM admin_settings WHERE key = ?").get(key) as { value: string } | undefined;
    return row?.value ?? def;
  } catch { return def; }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function setSetting(db: any, key: string, value: string): void {
  db.prepare(
    "INSERT INTO admin_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
  ).run(key, value);
}
BOTDBEOF
}

# ══════════════════════════════════════════════════════════════
#  Web Panel installer — نسخه Hybrid Stable
# ══════════════════════════════════════════════════════════════
_install_webpanel() {
  local _wp_path="$1" _wp_user="$2" _wp_pass="$3" _wp_port="$4"

  # ── 1. Node.js & pnpm ─────────────────────────────────────
  _step "Node.js & pnpm"
  _install_nodejs || return 1
  _install_pnpm   || return 1
  _ensure_unzip

  # ── 2. Locate nexora-panel.zip ────────────────────────────
  _step "Locating panel source..."
  local _panel_zip=""
  for _c in \
      "$SCRIPT_DIR/nexora-panel.zip" \
      "$BOT_DIR/nexora-panel.zip"; do
    if [[ -f "$_c" ]]; then
      _panel_zip="$_c"
      _ok "Found: $_panel_zip"
      break
    fi
  done

  if [[ -z "$_panel_zip" ]]; then
    _info "nexora-panel.zip not found locally — downloading..."
    _ensure_dns "github.com"
    local _dl="/tmp/nexora-panel-dl.zip"
    if _retry 3 10 curl -fsSL --progress-bar "$PANEL_RELEASE_URL" -o "$_dl"; then
      _panel_zip="$_dl"
      _ok "Downloaded nexora-panel.zip"
    else
      _err "Download failed from: $PANEL_RELEASE_URL"
      _err "Fix: place nexora-panel.zip next to install.sh and re-run"
      return 1
    fi
  fi

  # ── 3. Extract panel files ─────────────────────────────────
  _step "Extracting panel files..."
  rm -rf "$PANEL_INSTALL_DIR"
  mkdir -p "$PANEL_INSTALL_DIR"

  if ! unzip -q "$_panel_zip" -d "$PANEL_INSTALL_DIR" 2>&1; then
    _err "Extraction failed: $_panel_zip"
    return 1
  fi

  if [[ ! -f "$PANEL_INSTALL_DIR/package.json" ]]; then
    _err "Invalid panel ZIP — package.json not found after extraction"
    ls "$PANEL_INSTALL_DIR" | head -5
    return 1
  fi
  _ok "Panel files extracted to $PANEL_INSTALL_DIR"

  # ── patch package.json — clean start script ────────────────
  local _pkgjson="$PANEL_INSTALL_DIR/package.json"
  if [[ -f "$_pkgjson" ]]; then
    python3 -c "
import json, sys
f = sys.argv[1]
pkg = json.load(open(f))
pkg['scripts']['start'] = 'next start'
json.dump(pkg, open(f,'w'), indent=2)
print('package.json start script cleaned')
" "$_pkgjson" 2>/dev/null || true
  fi

  # ── patch next.config.ts — allowedDevOrigins: ["*"] ───────
  local _ncfg="$PANEL_INSTALL_DIR/next.config.ts"
  if [[ -f "$_ncfg" ]]; then
    python3 -c "
import re, sys
f = sys.argv[1]
c = open(f).read()
if '\"*\"' not in c:
    c = re.sub(r'allowedDevOrigins:\s*\[[^\]]*\]', 'allowedDevOrigins: [\"*\"]', c, flags=re.DOTALL)
    open(f, 'w').write(c)
    print('next.config.ts patched: allowedDevOrigins -> [\"*\"]')
else:
    print('next.config.ts already has wildcard origin')
" "$_ncfg" 2>/dev/null || _warn "Could not patch next.config.ts"
  fi

  # ── patch lib/xui.ts — v3 (redirect + form-encoded login) ─────
  # مشکل اصلی: نسخه اصلی xui.ts با redirect: "follow" (پیش‌فرض)
  # Set-Cookie header را گم می‌کند. همچنین بعضی نسخه‌های 3X-UI
  # فقط form-encoded login قبول می‌کنند نه JSON.
  local _xui_dir="$PANEL_INSTALL_DIR/lib"
  local _xui_ts="$_xui_dir/xui.ts"
  if [[ -d "$_xui_dir" ]]; then
    _info "Patching lib/xui.ts (v3 — redirect fix + form-encoded fallback)..."
    _patch_xui_ts "$_xui_ts"
    _ok "lib/xui.ts patched ✓"

    # ── patch lib/botdb.ts — dynamic require() به جای static import ──
    # مشکل: import Database from "better-sqlite3" در module-level — اگه
    # native addon به هر دلیل (ABI mismatch، npm vs pnpm) load نشه،
    # کل module crash می‌کنه و DB هرگز خوانده نمی‌شه. با require() فقط
    # getBotDb null برمی‌گرده و خطا در لاگ journalctl قابل مشاهده است.
    _info "Patching lib/botdb.ts (dynamic require + error logging)..."
    _patch_botdb_ts "$_xui_dir/botdb.ts"
    _ok "lib/botdb.ts patched ✓"
  else
    _warn "lib/ directory not found — skipping patches"
  fi

  # ── 4. Write .env.local BEFORE build ──────────────────────
  _step "Writing panel configuration..."
  local _bt="" _ai="" _pu="" _pn="admin" _pp=""
  local _vol_db="/var/lib/docker/volumes/nexora_bot_data/_data/bot_data.db"

  _bt="${BOT_TOKEN:-}"; _ai="${ADMIN_IDS:-}"
  _pu="${PANEL_URL:-}"; _pn="${PANEL_USERNAME:-admin}"; _pp="${PANEL_PASSWORD:-}"

  for _c in "$BOT_DIR/.env" "/opt/nexora-bot/.env" "/opt/nexora-bot/data/.env"; do
    if [[ -f "$_c" ]] && grep -q "^BOT_TOKEN=" "$_c" 2>/dev/null; then
      _info "Reading env from: $_c"
      _parse(){ grep "^$1=" "$_c" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'"; }
      [[ -z "$_bt" ]] && _bt=$(_parse BOT_TOKEN)
      [[ -z "$_ai" ]] && _ai=$(_parse ADMIN_IDS)
      [[ -z "$_pu" ]] && _pu=$(_parse PANEL_URL)
      [[ -z "$_pp" ]] && _pp=$(_parse PANEL_PASSWORD)
      local _fn; _fn=$(_parse PANEL_USERNAME); [[ -n "$_fn" ]] && _pn="$_fn"
      break
    fi
  done

  if [[ -z "$_pu" ]]; then
    _warn "PANEL_URL not found automatically."
    _ask "PANEL_URL (e.g. https://your-server:8443/webpath)" _pu
  fi

  # ── پیدا کردن مسیر واقعی DB (نه فرض کردن) ───────────────────────
  # Docker volume ممکنه با اسم متفاوت یا در مسیر متفاوت باشه
  # این کد مسیر واقعی را detect می‌کند
  local _real_db=""
  local _db_candidates=(
    "/var/lib/docker/volumes/nexora_bot_data/_data/bot_data.db"
    "/var/lib/docker/volumes/nexora_bot_data/_data/bot.db"
    "/opt/nexora-bot/data/bot_data.db"
    "/opt/nexora-bot/data/bot.db"
  )

  # اگه Docker volume وجود داره، فایل‌های داخلش رو هم چک کن
  if [[ -d "/var/lib/docker/volumes/nexora_bot_data/_data" ]]; then
    local _found_in_vol
    _found_in_vol=$(find /var/lib/docker/volumes/nexora_bot_data/_data/ -name "*.db" 2>/dev/null | head -1 || true)
    [[ -n "$_found_in_vol" ]] && _db_candidates=("$_found_in_vol" "${_db_candidates[@]}")
  fi

  for _c in "${_db_candidates[@]}"; do
    if [[ -f "$_c" ]]; then
      _real_db="$_c"
      _ok "Found bot DB at: $_real_db"
      break
    fi
  done

  if [[ -z "$_real_db" ]]; then
    _warn "Bot DB not found yet — using default path (bot may not be running)"
    _warn "DB will be found automatically after bot starts"
    _real_db="$_vol_db"
  fi

  cat > "$PANEL_INSTALL_DIR/.env.local" <<ENVEOF
NODE_ENV=production
WEB_PANEL_PATH=${_wp_path}
WEB_PANEL_USER=${_wp_user}
WEB_PANEL_PASS=${_wp_pass}
WEB_PANEL_PORT=${_wp_port}
WEB_PANEL_ENABLED=true
BOT_TOKEN=${_bt}
ADMIN_IDS=${_ai}
PANEL_URL=${_pu}
PANEL_USERNAME=${_pn}
PANEL_PASSWORD=${_pp}
BOT_DB_PATH=${_real_db}
# TLS bypass برای پنل‌های با self-signed certificate (مثل 3X-UI)
NODE_TLS_REJECT_UNAUTHORIZED=0
ENVEOF
  chmod 600 "$PANEL_INSTALL_DIR/.env.local"
  _ok "Config written"
  _info "  PANEL_URL  = ${_pu:-❌ empty — set later in Security page}"
  _info "  DB_PATH    = ${_real_db}"

  # ── 5. Install dependencies — Hybrid Stable ───────────────
  _step "Installing dependencies (may take 2-5 min on low-RAM servers)..."
  cd "$PANEL_INSTALL_DIR"

  hash -r 2>/dev/null || true
  local _pnpm_bin="/usr/local/bin/pnpm"
  [[ -x "$_pnpm_bin" ]] || _pnpm_bin=$(command -v pnpm 2>/dev/null || echo "")
  [[ -n "$_pnpm_bin" && -x "$_pnpm_bin" ]] || { _err "pnpm not found"; return 1; }
  _info "Using pnpm: $_pnpm_bin v$(${_pnpm_bin} --version 2>/dev/null)"

  # حذف lockfile های قدیمی
  rm -f pnpm-lock.yaml package-lock.json 2>/dev/null || true

  # ── Swap موقت 1GB برای سرورهای کم‌حافظه ─────────────────────────
  local _total_mem _swap_mem
  _total_mem=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
  _swap_mem=$(awk '/SwapTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
  local _swap_file="/swapfile_nexora_tmp"
  local _swap_added=false

  if (( (_total_mem + _swap_mem) < 1500000 )); then
    _info "Low memory detected ($(( _total_mem / 1024 ))MB RAM + $(( _swap_mem / 1024 ))MB swap)"
    _info "Creating temporary 1GB swap file for installation..."
    if ! swapon --show | grep -q "$_swap_file" 2>/dev/null; then
      fallocate -l 1G "$_swap_file" 2>/dev/null || dd if=/dev/zero of="$_swap_file" bs=1M count=1024 2>/dev/null || true
      chmod 600 "$_swap_file" 2>/dev/null || true
      mkswap "$_swap_file" 2>/dev/null && swapon "$_swap_file" 2>/dev/null && {
        _swap_added=true
        _ok "Temporary swap added (1GB)"
      } || _warn "Could not add swap — proceeding anyway"
    fi
  else
    _info "Memory OK: $(( _total_mem / 1024 ))MB RAM + $(( _swap_mem / 1024 ))MB swap"
  fi

  # ── نصب gcc اگه نیست (برای better-sqlite3 compile) ────────
  if ! command -v gcc &>/dev/null; then
    _info "Installing gcc for native addons..."
    case "$PKG_MANAGER" in
      apt) apt-get install -y -qq gcc g++ make 2>/dev/null | tail -1 ;;
      dnf) dnf install -y gcc gcc-c++ make 2>/dev/null | tail -1 ;;
      yum) yum install -y gcc gcc-c++ make 2>/dev/null | tail -1 ;;
    esac
    command -v gcc &>/dev/null && _ok "gcc installed" || _warn "gcc install failed"
  fi

  # ── تنظیم .npmrc با retry و timeout مناسب ─────────────────
  cat > .npmrc <<'NPMRCEOF'
fetch-retries=5
fetch-retry-mintimeout=2000
fetch-retry-maxtimeout=15000
fetch-timeout=60000
NPMRCEOF

  # ══════════════════════════════════════════════════════════
  # patch package.json — رفع مشکلات شناخته‌شده قبل از install
  # ══════════════════════════════════════════════════════════
  _info "→ Patching package.json for VPS stability..."
  node - <<'JSEOF'
const fs = require('fs');
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
const log = [];

// ── 1. حذف Cloudflare-only packages ───────────────────────────────
// wrangler@4.x نیاز به @cloudflare/workers-types@^5 دارد ولی در package.json
// نسخه ^4 هست → peer conflict غیرقابل‌حل برای npm و pnpm.
// این پکیج‌ها فقط برای deploy به CF Workers هستند، روی VPS استفاده ندارند.
const cfRemove = [
  'wrangler',
  '@cloudflare/workers-types',
  '@opennextjs/cloudflare',
  'agentation',
  '@vercel/analytics',   // vercel-only، روی VPS ارور یا warning می‌دهد
];
for (const k of cfRemove) {
  if (pkg.devDependencies?.[k])  { delete pkg.devDependencies[k];  log.push(`removed dev: ${k}`); }
  if (pkg.dependencies?.[k])     { delete pkg.dependencies[k];     log.push(`removed dep: ${k}`); }
}

// ── 2. fix better-sqlite3 — ریشه اصلی Worker crash ────────────────
// نسخه ^9.6.0 برای Node 22 prebuild binary ندارد و باید از source compile
// شود. prebuild-install (deprecated) این کار را می‌کند و در worker thread
// پنم crash می‌کند. نسخه 12.x کاملاً با Node 22 compatible است و
// prebuilt binary دارد — نیازی به compile نیست.
if (pkg.dependencies?.['better-sqlite3']) {
  pkg.dependencies['better-sqlite3'] = '^12.11.1';
  log.push('upgraded: better-sqlite3 ^9 → ^12.11.1 (Node 22 prebuilt)');
}
if (pkg.devDependencies?.['@types/better-sqlite3']) {
  pkg.devDependencies['@types/better-sqlite3'] = '^7.6.13';
  // نگه داری آخرین types که با v12 compatible است
}

// ── 3. fix recharts — deprecated 2.x ──────────────────────────────
// recharts@2.15.0 deprecated است؛ نسخه 3.x stable و بدون warning.
// API در 3.x تغییراتی دارد ولی برای next build مشکلی ایجاد نمی‌کند.
if (pkg.dependencies?.['recharts']) {
  const cur = pkg.dependencies['recharts'];
  // فقط اگه نسخه 2.x باشد upgrade کن
  if (cur.startsWith('2') || cur.includes('2.')) {
    pkg.dependencies['recharts'] = '^3.0.0';
    log.push('upgraded: recharts 2.x → ^3.0.0 (stable, no deprecated warning)');
  }
}

// ── 4. fix eslint-config-next version mismatch ────────────────────
// eslint-config-next باید همیشه با نسخه next یکسان باشد
if (pkg.devDependencies?.['eslint-config-next'] && pkg.dependencies?.['next']) {
  const nextVer = pkg.dependencies['next'].replace(/[\^~>=<]/g,'').split('.')[0];
  const eslintNextVer = pkg.devDependencies['eslint-config-next'].replace(/[\^~>=<]/g,'').split('.')[0];
  if (nextVer !== eslintNextVer) {
    pkg.devDependencies['eslint-config-next'] = pkg.dependencies['next'];
    log.push(`synced: eslint-config-next → ${pkg.dependencies['next']} (match next version)`);
  }
}

fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
if (log.length) {
  console.log('package.json patched:');
  log.forEach(l => console.log('  ✔ ' + l));
} else {
  console.log('package.json already clean — no changes needed');
}
JSEOF

  # ── نصب dependencies ─────────────────────────────────────────────
  # روش ۱: pnpm با NODE_OPTIONS + --concurrency=1
  #   --concurrency=1 جلوگیری از Worker thread parallelism crash
  #   (باگ pnpm 9.x با native addon هایی که نیاز به compile دارند)
  # روش ۲: npm با --legacy-peer-deps (fallback اگه pnpm crash کرد)
  # روش ۳: pnpm با --force + --concurrency=1 (آخرین تلاش)
  local _install_ok=false
  local _used_npm=false   # flag: اگه npm fallback استفاده شد باید rebuild بزنیم

  _info "→ Trying pnpm install (concurrency=1 to prevent Worker crash)..."
  if NODE_OPTIONS="--max-old-space-size=512" \
      "${_pnpm_bin}" install --no-frozen-lockfile --ignore-scripts \
        --config.concurrency=1 2>&1; then
    _install_ok=true
    _ok "pnpm install succeeded"
  else
    _warn "pnpm failed — falling back to npm install --legacy-peer-deps..."
    rm -f pnpm-lock.yaml 2>/dev/null || true
    if npm install --ignore-scripts --legacy-peer-deps 2>&1; then
      _install_ok=true
      _used_npm=true
      _ok "npm install succeeded (fallback)"
    else
      _warn "npm also failed — trying pnpm --force + concurrency=1 as last resort..."
      if NODE_OPTIONS="--max-old-space-size=512" \
          "${_pnpm_bin}" install --no-frozen-lockfile --ignore-scripts \
            --force --config.concurrency=1 2>&1; then
        _install_ok=true
        _ok "pnpm install --force succeeded"
      fi
    fi
  fi

  if [[ "$_install_ok" != "true" ]]; then
    _err "All install methods failed"
    return 1
  fi

  # ── build better-sqlite3 native binary (فقط اگه npm fallback بود) ──
  # pnpm با --ignore-scripts مشکل rebuild ندارد چون native پکیج‌ها را
  # از prebuilt binary می‌گیرد. npm با --ignore-scripts postinstall را
  # skip می‌کند و better_sqlite3.node ساخته نمی‌شود — پس باید rebuild بزنیم.
  if [[ "$_used_npm" == "true" ]]; then
    _info "→ npm was used — rebuilding better-sqlite3 native binary..."

    # تست اول — شاید prebuilt binary دانلود شده باشد
    if node -e "require('${PANEL_INSTALL_DIR}/node_modules/better-sqlite3')" 2>/dev/null; then
      _ok "better-sqlite3 binary OK (prebuilt) ✓"
    else
      # node-gyp نصب نیست؟
      command -v node-gyp &>/dev/null || npm install -g node-gyp 2>/dev/null | tail -1 || true
      if npm rebuild better-sqlite3 2>&1 | tail -5; then
        _ok "better-sqlite3 native binary built ✓"
      else
        _warn "npm rebuild failed — trying node-gyp directly..."
        (cd "${PANEL_INSTALL_DIR}/node_modules/better-sqlite3" && \
          node-gyp rebuild 2>&1 | tail -5) || _warn "node-gyp also failed"
      fi

      # تست نهایی
      if node -e "require('${PANEL_INSTALL_DIR}/node_modules/better-sqlite3')" 2>/dev/null; then
        _ok "better-sqlite3 verified ✓"
      else
        _err "better-sqlite3 binary still missing — DB will not work"
        _err "Manual fix: cd ${PANEL_INSTALL_DIR} && npm rebuild better-sqlite3"
      fi
    fi
  else
    _info "→ pnpm was used — skipping better-sqlite3 rebuild (not needed)"
  fi

  _ok "Dependencies ready"

  # ── 6. Build panel ────────────────────────────────────────
  _step "Building panel (~1-2 min)..."
  NODE_ENV=production NODE_OPTIONS="--max-old-space-size=512" \
    "${_pnpm_bin}" build 2>&1 | tail -8
  [[ -d "$PANEL_INSTALL_DIR/.next" ]] || { _err "Build failed — .next not created"; return 1; }
  _ok "Panel built successfully"

  # ── حذف swap موقت بعد از build ────────────────────────────
  if [[ "${_swap_added:-false}" == "true" ]] && [[ -f "$_swap_file" ]]; then
    _info "Removing temporary swap file..."
    swapoff "$_swap_file" 2>/dev/null || true
    rm -f "$_swap_file" 2>/dev/null || true
    _ok "Temporary swap removed"
  fi

  # ── 7. systemd service — با pnpm start ────────────────────
  _step "Creating systemd service..."
  local _pnpm_bin_abs
  _pnpm_bin_abs=$(command -v pnpm 2>/dev/null || echo "$PNPM_HOME/pnpm")
  if [[ ! -f /usr/local/bin/pnpm ]]; then
    ln -sf "$_pnpm_bin_abs" /usr/local/bin/pnpm 2>/dev/null || true
    _pnpm_bin_abs="/usr/local/bin/pnpm"
  fi

  # ExecStart از pnpm start استفاده می‌کنه (نه next start مستقیم)
  cat > /etc/systemd/system/nexora-panel.service <<SVCEOF
[Unit]
Description=Nexora Web Admin Panel
After=network.target

[Service]
Type=simple
WorkingDirectory=${PANEL_INSTALL_DIR}
ExecStart=${_pnpm_bin_abs} start
Restart=always
RestartSec=5
EnvironmentFile=${PANEL_INSTALL_DIR}/.env.local
Environment=NODE_ENV=production
Environment=PORT=${_wp_port}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

  systemctl daemon-reload
  systemctl enable nexora-panel 2>/dev/null
  systemctl stop   nexora-panel 2>/dev/null || true
  sleep 1
  systemctl start  nexora-panel
  sleep 5

  if systemctl is-active nexora-panel &>/dev/null; then
    _ok "Panel service running ✓"
  else
    _warn "Service may have issues. Check: journalctl -u nexora-panel -n 30"
    journalctl -u nexora-panel -n 8 --no-pager 2>/dev/null || true
  fi

  # ── اگه npm استفاده شد، rebuild + restart بعد از start ───────
  # دلیل: build قبل از rebuild انجام شد، پس .next binary references
  # اشتباه دارد. یک restart کافیه چون rebuild قبلاً انجام شده.
  if [[ "${_used_npm:-false}" == "true" ]]; then
    _info "npm was used — running final rebuild + restart to ensure DB works..."
    cd "$PANEL_INSTALL_DIR"
    npm rebuild better-sqlite3 2>&1 | tail -2 || true
    sleep 1
    systemctl restart nexora-panel
    sleep 3
    if systemctl is-active nexora-panel &>/dev/null; then
      _ok "Panel restarted after npm rebuild ✓"
    fi
    cd "$BOT_DIR" 2>/dev/null || true
  fi

  # ── post-start diagnostic ─────────────────────────────────
  _step "Running connectivity diagnostics..."

  # ① DB check
  local _db_check="${_real_db}"
  if [[ -f "$_db_check" ]]; then
    _ok "DB file exists: $_db_check ($(du -sh "$_db_check" 2>/dev/null | cut -f1))"
    # permission check — nexora-panel با root اجرا می‌شه، باید readable باشه
    if [[ -r "$_db_check" ]]; then
      _ok "DB file is readable ✓"
    else
      _warn "DB file not readable — fixing permissions..."
      chmod 644 "$_db_check" 2>/dev/null && _ok "DB permissions fixed" || \
        _warn "Could not fix DB permissions"
    fi
  else
    _warn "DB file not found at: $_db_check"
    _warn "This is normal if bot hasn't run yet. Bot creates DB on first start."
    # اگه bot در حال اجرا است، کمی صبر کنیم
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "nexora_bot"; then
      _info "Waiting 5s for bot to create DB..."
      sleep 5
      [[ -f "$_db_check" ]] && _ok "DB created: $_db_check" || \
        _warn "DB still not found — panel will show 'database unavailable' until bot creates it"
    fi
  fi

  # ② PANEL_URL connectivity check (اگه تنظیم شده)
  if [[ -n "${_pu:-}" ]]; then
    _info "Testing connection to 3X-UI panel..."
    local _xui_http_code
    _xui_http_code=$(curl -sk --max-time 10 -o /dev/null -w "%{http_code}" \
      "${_pu%/}/" 2>/dev/null || echo "000")
    if [[ "$_xui_http_code" =~ ^(200|301|302|303|307|308)$ ]]; then
      _ok "3X-UI panel reachable (HTTP $_xui_http_code) ✓"
    elif [[ "$_xui_http_code" == "000" ]]; then
      _warn "3X-UI panel not reachable from this server (timeout/network)"
      _warn "URL: ${_pu}"
      _warn "Panel dashboard will show 'panel unavailable' — check:"
      _warn "  1. پنل 3X-UI در حال اجرا است؟"
      _warn "  2. پورت ${_pu##*:} در فایروال سرور پنل باز است؟"
      _warn "  3. آدرس PANEL_URL درست است؟"
    else
      _info "3X-UI panel responded HTTP $_xui_http_code (may need login)"
    fi
  fi

  # ── 8. Firewall ───────────────────────────────────────────
  if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "active"; then
    ufw allow "${_wp_port}/tcp" &>/dev/null && _ok "UFW: port ${_wp_port} opened"
  elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="${_wp_port}/tcp" &>/dev/null && \
      firewall-cmd --reload &>/dev/null && _ok "firewalld: port ${_wp_port} opened"
  fi

  # ── 9. Optional nginx SSL ─────────────────────────────────
  echo ""
  _line
  echo -e "  ${BOLD}🔒  HTTPS / nginx  ${DIM}(optional)${RESET}"
  echo -e "  ${DIM}Set up nginx + Let's Encrypt SSL for HTTPS access?${RESET}"
  read -rp "  Setup nginx HTTPS? (y/N): " _nx
  if [[ "$_nx" =~ ^[Yy]$ ]]; then
    read -rp "  Domain (e.g. panel.example.com): " _dom
    if [[ -n "$_dom" ]]; then
      _setup_nginx_ssl "$_dom" "$_wp_port"
    fi
  fi

  cd "$BOT_DIR" 2>/dev/null || true
}

_setup_nginx_ssl() {
  local _domain="$1" _port="$2"
  local _nginx_conf="/etc/nginx/conf.d/nexora-panel.conf"

  case "$PKG_MANAGER" in
    apt) _retry 3 5 apt-get install -y -qq nginx certbot python3-certbot-nginx 2>&1 | tail -2 ;;
    dnf) _retry 3 5 dnf install -y nginx certbot python3-certbot-nginx 2>&1 | tail -2 ;;
    yum) _retry 3 5 yum install -y nginx certbot python3-certbot-nginx 2>&1 | tail -2 ;;
  esac

  # HTTP-only config first (for ACME challenge)
  cat > "$_nginx_conf" <<NGINXEOF
server {
    listen 80;
    server_name ${_domain};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / {
        proxy_pass http://127.0.0.1:${_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_cache_bypass \$http_upgrade;
    }
}
NGINXEOF

  systemctl enable nginx --now 2>/dev/null || true
  nginx -t 2>/dev/null && systemctl reload nginx && _ok "nginx (HTTP) configured"

  if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "active"; then
    ufw allow 80/tcp &>/dev/null; ufw allow 443/tcp &>/dev/null
  elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-service={http,https} &>/dev/null && firewall-cmd --reload &>/dev/null
  fi

  _info "Getting SSL certificate for ${_domain}..."
  mkdir -p /var/www/html
  # پاک‌سازی certbot lock در صورت وجود
  rm -f /tmp/certbot-*.lock /tmp/.certbot.lock /var/lib/letsencrypt/.certbot.lock 2>/dev/null || true
  pkill -f "certbot" 2>/dev/null || true; sleep 1

  if certbot certonly --webroot -w /var/www/html -d "$_domain" \
      --non-interactive --agree-tos --register-unsafely-without-email 2>&1 | tail -5; then

    local _cert_dir="/etc/letsencrypt/live/${_domain}"
    for _old in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/*; do
      [[ -f "$_old" && "$_old" != "$_nginx_conf" ]] || continue
      grep -q "server_name.*${_domain}" "$_old" 2>/dev/null && \
        { _warn "Removing conflicting config: $_old"; rm -f "$_old"; }
    done
    cat > "$_nginx_conf" <<NGINXSSL
server {
    listen 80;
    server_name ${_domain};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name ${_domain};
    ssl_certificate     ${_cert_dir}/fullchain.pem;
    ssl_certificate_key ${_cert_dir}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    location /webhook/ {
        proxy_pass http://127.0.0.1:${WEBHOOK_PORT:-9988};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 15s;
    }
    location /health { proxy_pass http://127.0.0.1:${WEBHOOK_PORT:-9988}/health; }
    location / {
        proxy_pass         http://127.0.0.1:${_port};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection 'upgrade';
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_cache_bypass \$http_upgrade;
    }
}
NGINXSSL
    nginx -t 2>/dev/null && systemctl reload nginx
    systemctl restart nexora-panel
    _ok "SSL ready: https://${_domain}"
  else
    _warn "SSL failed — check DNS for ${_domain} → this server IP"
    _warn "Retry: certbot certonly --webroot -w /var/www/html -d ${_domain}"
  fi
}

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
_banner
echo -e "  This script installs Nexora VPN Bot on your server."
echo -e "  ${DIM}Prerequisites: 3X-UI panel must already be running.${RESET}"
echo ""
read -rp "  Press Enter to start or Ctrl+C to cancel..." _

# ──────────────────────────────────────────────────────────────
#  STEP 1  Network & Docker
# ──────────────────────────────────────────────────────────────
_header "STEP 1 — Network & Docker"
_detect_os
_info "OS: ${OS_ID:-unknown}  |  Package manager: ${PKG_MANAGER}"

_step "Checking DNS..."
_ensure_dns "download.docker.com"
_ensure_dns "github.com"

_step "Installing base utilities..."

# ── fix dpkg interrupted state (اگه نصب قبلی ناقص مانده) ──────
if [[ "$PKG_MANAGER" == "apt" ]]; then
  if apt-get check 2>&1 | grep -qi "dpkg was interrupted\|unmet dep\|broken"; then
    _warn "dpkg interrupted state detected — fixing..."
    dpkg --configure -a 2>&1 | tail -3 || true
    apt-get install -f -y 2>&1 | tail -3 || true
    _ok "dpkg state fixed"
  fi
  # unattended-upgrades را موقتاً متوقف کن تا lock آزاد بشه
  systemctl stop unattended-upgrades 2>/dev/null || true
  # صبر برای آزاد شدن apt lock
  _w=0
  while fuser /var/lib/dpkg/lock-frontend &>/dev/null 2>&1 || \
        fuser /var/lib/apt/lists/lock      &>/dev/null 2>&1; do
    sleep 2; (( _w+=2 ))
    if (( _w >= 60 )); then
      _warn "apt lock held too long — forcing release..."
      rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
            /var/cache/apt/archives/lock /var/lib/apt/lists/lock 2>/dev/null || true
      dpkg --configure -a 2>/dev/null || true
      break
    fi
    _progress "Waiting for apt lock... (${_w}s)"
  done
fi

_pkgs_apt=(curl git openssl unzip); _pkgs_rpm=(curl git openssl unzip)
command -v rsync  &>/dev/null || { _pkgs_apt+=(rsync);    _pkgs_rpm+=(rsync); }
command -v getent &>/dev/null || { _pkgs_apt+=(libc-bin); _pkgs_rpm+=(glibc-common); }
case "$PKG_MANAGER" in
  apt) _retry 3 5 apt-get install -y -qq "${_pkgs_apt[@]}" 2>/dev/null | tail -1 ;;
  dnf) _retry 3 5 dnf install -y "${_pkgs_rpm[@]}" 2>/dev/null | tail -1 ;;
  yum) _retry 3 5 yum install -y "${_pkgs_rpm[@]}" 2>/dev/null | tail -1 ;;
esac
_ok "Base utilities ready"

_step "Checking Docker..."
if command -v docker &>/dev/null; then
  _ok "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
else
  _install_docker
fi
_ensure_compose

if ! docker info &>/dev/null 2>&1; then
  _warn "Docker daemon not running — starting..."
  systemctl start docker 2>/dev/null && sleep 3
  docker info &>/dev/null 2>&1 || { _err "Docker daemon failed to start"; exit 1; }
fi
_ok "Docker daemon: running"

# ──────────────────────────────────────────────────────────────
#  STEP 2  Install directory
# ──────────────────────────────────────────────────────────────
_header "STEP 2 — Install Directory"
DEFAULT_DIR="/opt/nexora-bot"
echo -e "   Default: ${CYAN}$DEFAULT_DIR${RESET}"
read -rp "   Press Enter for default or type a custom path: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
INSTALL_DIR="$(realpath -m "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"

BOT_DIR="$INSTALL_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  _warn "Directory exists: $INSTALL_DIR"
  read -rp "   Continue? Will pull latest files. (y/N): " _yn
  [[ "$_yn" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi
_ok "Install directory: $INSTALL_DIR"

# ──────────────────────────────────────────────────────────────
#  STEP 3  Get bot files
# ──────────────────────────────────────────────────────────────
_header "STEP 3 — Bot Files"
BOT_REPO="https://github.com/MNSH-Nexo/dev-nexora-bot.git"
_ensure_dns "github.com"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  _info "Updating existing installation..."
  git -C "$INSTALL_DIR" fetch origin 2>&1 | tail -1
  git -C "$INSTALL_DIR" reset --hard origin/main 2>&1 | tail -1
  _ok "Updated: $INSTALL_DIR"
else
  rm -rf "$INSTALL_DIR"
  _retry 3 10 git clone --depth 1 "$BOT_REPO" "$INSTALL_DIR"
  _ok "Cloned to: $INSTALL_DIR"
fi
cd "$BOT_DIR"

# Cleanup old conflicting containers
_step "Cleaning up old containers..."
for _c in vpn_bot vpn_postgres nexora_bot nexora_postgres; do
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${_c}$" && \
    { _warn "Removing old container: $_c"; docker rm -f "$_c" 2>/dev/null || true; }
done
for _n in vpn_network nexora_network; do
  docker network ls --format '{{.Name}}' 2>/dev/null | grep -q "^${_n}$" && \
    { _warn "Removing old network: $_n"; docker network rm "$_n" 2>/dev/null || true; }
done
_ok "Cleanup done"

# ──────────────────────────────────────────────────────────────
#  STEP 4  Configuration
# ──────────────────────────────────────────────────────────────
_header "STEP 4 — Configuration"
_line
echo -e "   ${DIM}Fill in the required settings.${RESET}"
echo -e "   ${DIM}You can change them later with: nexo-bot → Configuration${RESET}"
echo ""

# Telegram Bot Token
echo -e "   ${BOLD}Telegram Bot Token${RESET}  ${DIM}(from @BotFather → /newbot)${RESET}"
_ask_s "BOT_TOKEN" BOT_TOKEN
while [[ -z "$BOT_TOKEN" ]]; do _warn "Cannot be empty"; _ask_s "BOT_TOKEN" BOT_TOKEN; done

# Admin Telegram ID
echo -e "\n   ${BOLD}Your Telegram ID (numeric)${RESET}  ${DIM}(get it from @userinfobot)${RESET}"
_ask "Admin ID(s) — comma-separated" ADMIN_IDS
while [[ -z "$ADMIN_IDS" ]]; do _warn "Cannot be empty"; _ask "Admin ID(s)" ADMIN_IDS; done

# Admin Secret
echo -e "\n   ${BOLD}Admin Secret Password${RESET}  ${DIM}(to login in bot: /admin_secret PASSWORD)${RESET}"
_ask_s "Admin secret" ADMIN_SECRET
while [[ -z "$ADMIN_SECRET" ]]; do _warn "Cannot be empty"; _ask_s "Admin secret" ADMIN_SECRET; done

# 3X-UI Panel
echo -e "\n   ${BOLD}3X-UI Panel URL${RESET}  ${DIM}(e.g. https://srv:8443/webpath)${RESET}"
_ask "PANEL_URL" PANEL_URL
while [[ -z "$PANEL_URL" ]]; do _warn "Cannot be empty"; _ask "PANEL_URL" PANEL_URL; done
PANEL_URL="${PANEL_URL%/}"

echo -e "\n   ${BOLD}3X-UI Panel Username${RESET}"
_ask "PANEL_USERNAME (default: admin)" PANEL_USERNAME
PANEL_USERNAME="${PANEL_USERNAME:-admin}"

echo -e "\n   ${BOLD}3X-UI Panel Password${RESET}"
_ask_s "PANEL_PASSWORD" PANEL_PASSWORD
while [[ -z "$PANEL_PASSWORD" ]]; do _warn "Cannot be empty"; _ask_s "PANEL_PASSWORD" PANEL_PASSWORD; done

# Sub Port
echo -e "\n   ${BOLD}Subscription Link Port${RESET}  ${DIM}(optional — default: 2096)${RESET}"
echo -e "   ${DIM}The port your 3X-UI serves subscription links on${RESET}"
_ask "SUB_PORT (default: 2096)" SUB_PORT
SUB_PORT="${SUB_PORT:-2096}"

# Database
echo -e "\n   ${BOLD}Database Type${RESET}"
echo -e "   ${GREEN}[1]${RESET} PostgreSQL  ${DIM}(recommended for production)${RESET}"
echo -e "   ${DIM}[2]${RESET} SQLite      ${DIM}(simple, fine for most setups)${RESET}"
read -rp "   Choose (1/2, default: 2): " DB_CHOICE
DB_CHOICE="${DB_CHOICE:-2}"
USE_POSTGRES=false
if [[ "$DB_CHOICE" == "1" ]]; then
  echo -e "\n   ${BOLD}PostgreSQL Password${RESET}"
  _ask_s "Password (Enter = auto-generate)" POSTGRES_PASSWORD
  if [[ -z "$POSTGRES_PASSWORD" ]]; then
    POSTGRES_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    _ok "Auto-generated password (saved in .env)"
  fi
  DB_URL="postgresql+asyncpg://botuser:${POSTGRES_PASSWORD}@nexora_postgres/vpn_bot"
  USE_POSTGRES=true
  _ok "Database: PostgreSQL"
else
  DB_URL="sqlite+aiosqlite:////app/data/bot_data.db"
  POSTGRES_PASSWORD=""; _ok "Database: SQLite"
fi

# Payments — NOWPayments
_line
echo -e "\n   ${BOLD}NOWPayments API Key${RESET}  ${DIM}(optional — press Enter to skip)${RESET}"
_ask_s "NOWPAYMENTS_API_KEY" NOWPAYMENTS_API_KEY
NOWPAYMENTS_IPN_SECRET=""; NOWPAYMENTS_IPN_URL=""; WEBHOOK_PORT="9988"; _SERVER_HOST=""

if [[ -n "$NOWPAYMENTS_API_KEY" ]]; then
  echo -e "\n   ${BOLD}NOWPayments IPN Secret${RESET}  ${DIM}(from NOWPayments → Settings → IPN Secret)${RESET}"
  _ask_s "IPN Secret (Enter to skip)" NOWPAYMENTS_IPN_SECRET
  if [[ -n "$NOWPAYMENTS_IPN_SECRET" ]]; then
    echo -e "\n   ${BOLD}Webhook Port${RESET}  ${DIM}(default: 9988)${RESET}"
    _ask "Webhook port (default: 9988)" WEBHOOK_PORT
    WEBHOOK_PORT="${WEBHOOK_PORT:-9988}"
    echo -e "\n   ${BOLD}Server Domain or IP${RESET}  ${DIM}(for webhook URL — no port/path)${RESET}"
    _ask "Domain or IP" _SERVER_HOST
    [[ -n "$_SERVER_HOST" ]] && \
      NOWPAYMENTS_IPN_URL="http://${_SERVER_HOST}:${WEBHOOK_PORT}/webhook/nowpayments" && \
      _ok "IPN URL: $NOWPAYMENTS_IPN_URL"
  fi
fi

# Payments — MaxelPay
_line
echo -e "\n   ${BOLD}MaxelPay API Key${RESET}  ${DIM}(optional crypto gateway — maxelpay.com)${RESET}"
_ask_s "MAXELPAY_API_KEY (Enter to skip)" MAXELPAY_API_KEY
MAXELPAY_WEBHOOK_URL=""; BOT_USERNAME=""
if [[ -n "$MAXELPAY_API_KEY" ]]; then
  echo -e "\n   ${BOLD}MaxelPay Webhook URL${RESET}  ${DIM}(must be HTTPS + publicly accessible)${RESET}"
  if [[ -n "$_SERVER_HOST" ]]; then
    local _sug="https://${_SERVER_HOST}:${WEBHOOK_PORT:-9988}/webhook/maxelpay"
    echo -e "   ${DIM}Suggested: ${CYAN}${_sug}${RESET}"
    _ask "URL (Enter for suggested)" _mx; MAXELPAY_WEBHOOK_URL="${_mx:-$_sug}"
  else
    _ask "MAXELPAY_WEBHOOK_URL" MAXELPAY_WEBHOOK_URL
  fi
  echo -e "\n   ${BOLD}Bot Username${RESET}  ${DIM}(without @ — for return link after payment)${RESET}"
  _ask "BOT_USERNAME (Enter to skip)" BOT_USERNAME
  BOT_USERNAME="${BOT_USERNAME#@}"
fi

# ──────────────────────────────────────────────────────────────
#  STEP 5  Write bot .env
# ──────────────────────────────────────────────────────────────
_header "STEP 5 — Writing .env"
cat > "$BOT_DIR/.env" <<EOF
# Nexora VPN Bot — auto-generated by installer
# Edit anytime with: nexo-bot → Configuration → Edit .env

BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
ADMIN_SECRET=${ADMIN_SECRET}

PANEL_URL=${PANEL_URL}
PANEL_USERNAME=${PANEL_USERNAME}
PANEL_PASSWORD=${PANEL_PASSWORD}
SUB_PORT=${SUB_PORT}

DB_URL=${DB_URL}
EOF

[[ "$USE_POSTGRES" == "true" ]] && echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" >> "$BOT_DIR/.env"

cat >> "$BOT_DIR/.env" <<EOF

DEFAULT_SUBSCRIPTION_DAYS=30
DEFAULT_TRAFFIC_GB=0

NOWPAYMENTS_API_KEY=${NOWPAYMENTS_API_KEY:-}
NOWPAYMENTS_IPN_SECRET=${NOWPAYMENTS_IPN_SECRET:-}
NOWPAYMENTS_IPN_URL=${NOWPAYMENTS_IPN_URL:-}
NOWPAYMENTS_PAY_CURRENCY=usdttrc20
INVOICE_EXPIRE_MINUTES=30
WEBHOOK_PORT=${WEBHOOK_PORT:-9988}

MAXELPAY_API_KEY=${MAXELPAY_API_KEY:-}
MAXELPAY_WEBHOOK_URL=${MAXELPAY_WEBHOOK_URL:-}
BOT_USERNAME=${BOT_USERNAME:-}

LOG_LEVEL=INFO
LOG_FILE=logs/bot.log
EOF

chmod 600 "$BOT_DIR/.env"
_ok ".env written (chmod 600)"

# ──────────────────────────────────────────────────────────────
#  STEP 6  Build & Start bot
# ──────────────────────────────────────────────────────────────
_header "STEP 6 — Build & Start Bot"
_step "Building Docker image..."
cd "$BOT_DIR"

_docker_build_bot || exit 1

_step "Starting services..."
if [[ "$USE_POSTGRES" == "true" ]]; then
  docker compose --profile postgres up -d
  _ok "PostgreSQL + Bot started"
  echo -e "   ${DIM}Waiting for PostgreSQL...${RESET}"
  for i in $(seq 1 20); do
    docker compose --profile postgres exec -T db pg_isready -U botuser -d vpn_bot &>/dev/null && \
      { _ok "PostgreSQL ready"; break; }
    sleep 2; echo -ne "   ${DIM}... ($i/20)${RESET}\r"
  done
else
  docker compose up -d bot
  _ok "Bot started (SQLite)"
fi

# ──────────────────────────────────────────────────────────────
#  STEP 7  nexo-bot CLI
# ──────────────────────────────────────────────────────────────
_header "STEP 7 — nexo-bot CLI"
chmod +x "$BOT_DIR/nexo-bot"
echo "USE_POSTGRES=${USE_POSTGRES}" >> "$BOT_DIR/.env"
ln -sf "$BOT_DIR/nexo-bot" /usr/local/bin/nexo-bot
_ok "Command installed: nexo-bot"

# ──────────────────────────────────────────────────────────────
#  STEP 8  Web Admin Panel (optional)
# ──────────────────────────────────────────────────────────────
_header "STEP 8 — Web Admin Panel"
echo -e "  ${DIM}A web dashboard to manage your bot from the browser.${RESET}"
echo -e "  ${DIM}Features: stats, plans, users, transactions, tickets, settings${RESET}"
echo ""
read -rp "  Install Web Admin Panel? (y/N): " _install_yn
_install_yn="${_install_yn:-N}"

WEB_PANEL_INSTALLED=false; WEB_PANEL_PATH_FINAL=""; WEB_PANEL_PORT_FINAL="3000"

if [[ "$_install_yn" =~ ^[Yy]$ ]]; then
  echo ""
  _rnd=$(tr -dc 'a-z0-9' </dev/urandom 2>/dev/null | head -c 12 || date +%s | sha256sum | head -c 12)
  echo -e "  ${BOLD}Security Path${RESET}  ${DIM}(only those with this URL can reach login)${RESET}"
  echo -e "  ${DIM}Suggestion: ${CYAN}/${_rnd}${RESET}  ${DIM}(press Enter to use)${RESET}"
  read -rp "  Web Path: " _wp; _wp="${_wp:-/${_rnd}}"; _wp="/${_wp#/}"

  read -rp "  Admin username (default: admin): " _wu; _wu="${_wu:-admin}"

  echo -e "  ${BOLD}Admin password${RESET}  ${DIM}(min 8 characters)${RESET}"
  while true; do
    read -rsp "  Password: " _wpass; echo ""
    [[ ${#_wpass} -ge 8 ]] && break
    _warn "Must be at least 8 characters"
  done
  read -rsp "  Confirm password: " _wpass2; echo ""

  if [[ "$_wpass" != "$_wpass2" ]]; then
    _err "Passwords do not match — skipping web panel"
    _warn "Install later: nexo-bot → [9] Web Panel"
  else
    read -rp "  Port (default: 3000): " _wport; _wport="${_wport:-3000}"
    _install_webpanel "$_wp" "$_wu" "$_wpass" "$_wport"
    WEB_PANEL_INSTALLED=true
    WEB_PANEL_PATH_FINAL="$_wp"
    WEB_PANEL_PORT_FINAL="$_wport"
    { echo ""; echo "# Web Panel"
      echo "WEB_PANEL_ENABLED=true"
      echo "WEB_PANEL_PATH=${_wp}"
      echo "WEB_PANEL_USER=${_wu}"
      echo "WEB_PANEL_PASS=${_wpass}"
      echo "WEB_PANEL_PORT=${_wport}"
    } >> "$BOT_DIR/.env"
  fi
else
  _info "Web panel skipped — install later: nexo-bot → [9] Web Panel"
fi

# ──────────────────────────────────────────────────────────────
#  DONE
# ──────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║       ✔  NEXORA VPN BOT — INSTALLATION COMPLETE         ║"
echo "  ║       Hybrid Stable v1.8 · Built by MNSH-Nexo           ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${RESET}"
echo -e "  ${BOLD}Bot installed at:${RESET}  ${CYAN}$INSTALL_DIR${RESET}"
echo -e "  ${BOLD}Manage with:${RESET}       ${CYAN}nexo-bot${RESET}"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "  1. Open Telegram → message your bot"
echo -e "  2. Login as admin: /admin_secret ****  ${DIM}(see ADMIN_SECRET in .env)${RESET}"
echo -e "  3. Type ${CYAN}nexo-bot${RESET} anytime to manage"
echo ""

if [[ "$WEB_PANEL_INSTALLED" == "true" ]]; then
  _get_server_ip() {
    local _raw
    _raw=$(curl -s4 --max-time 5 ifconfig.me 2>/dev/null || \
           curl -s4 --max-time 5 icanhazip.com 2>/dev/null || \
           curl -s4 --max-time 5 api.ipify.org 2>/dev/null || true)
    if echo "$_raw" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
      echo "$_raw"
    else
      hostname -I 2>/dev/null | awk '{print $1}' || echo "YOUR_IP"
    fi
  }
  _ip=$(_get_server_ip)
  echo -e "  ${BOLD}🌐  Web Panel:${RESET}"
  echo -e "  • URL  : ${CYAN}http://${_ip}:${WEB_PANEL_PORT_FINAL}${WEB_PANEL_PATH_FINAL}${RESET}"
  echo -e "  • Path : ${YELLOW}${WEB_PANEL_PATH_FINAL}${RESET}  ${DIM}← keep this secret!${RESET}"
  echo -e "  • Logs : ${CYAN}journalctl -u nexora-panel -f${RESET}"
  if [[ -d /etc/letsencrypt/live ]]; then
    _dom=$(ls /etc/letsencrypt/live/ 2>/dev/null | grep -v "^README$" | head -1 || true)
    [[ -n "$_dom" ]] && echo -e "  • HTTPS: ${CYAN}https://${_dom}${WEB_PANEL_PATH_FINAL}${RESET}"
  fi
  echo ""
fi

echo -e "  ${DIM}──────────────────────────────────────────────────────${RESET}"
echo -e "  ${DIM}Useful commands:${RESET}"
echo -e "  • Bot logs   : ${CYAN}docker compose -f $INSTALL_DIR/docker-compose.yml logs -f bot${RESET}"
echo -e "  • Panel logs : ${CYAN}journalctl -u nexora-panel -f${RESET}"
echo -e "  • Restart bot: ${CYAN}docker compose -f $INSTALL_DIR/docker-compose.yml restart bot${RESET}"
echo ""
echo -e "  ${DIM}Built by MNSH-Nexo · github.com/MNSH-Nexo${RESET}"
echo ""
