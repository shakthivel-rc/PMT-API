# shellcheck shell=bash
#
# Finding, starting and — where it is safe — installing the things setup needs.
#
# The rule this file follows: prefer *using* something that is already there, then prefer
# *starting* it, and only then install. Installing is the most invasive answer and the one
# most likely to need a password, a reboot or a network the machine may not have.
#
# Nothing here installs without naming the exact command first. `SETUP_AUTO_INSTALL=0`
# turns installation off entirely and leaves the diagnosis in place.
#
# This file is held to the portability contract at the top of common.sh.

[ -n "${NX_DEPS_SH:-}" ] && return 0
NX_DEPS_SH=1

# --- platform ---------------------------------------------------------------------------

# linux | macos | windows | unknown. "windows" here means a POSIX shell hosted on Windows
# — Git Bash or MSYS2 — which is what scripts/bootstrap.ps1 hands control to. WSL reports
# itself as Linux and genuinely is one, so it needs no special case.
nx_os() {
    case "$(uname -s 2>/dev/null || echo unknown)" in
        Linux*)  printf 'linux' ;;
        Darwin*) printf 'macos' ;;
        MINGW*|MSYS*|CYGWIN*) printf 'windows' ;;
        *) printf 'unknown' ;;
    esac
}

nx_arch() {
    case "$(uname -m 2>/dev/null || echo unknown)" in
        x86_64|amd64) printf 'amd64' ;;
        arm64|aarch64) printf 'arm64' ;;
        *) printf 'other' ;;
    esac
}

# apt | dnf | yum | pacman | zypper | apk | brew | none
nx_pkg_manager() {
    if [ "$(nx_os)" = "macos" ]; then
        nx_have brew && { printf 'brew'; return 0; }
        printf 'none'; return 0
    fi
    for candidate in apt-get dnf yum pacman zypper apk; do
        if nx_have "$candidate"; then
            case "$candidate" in
                apt-get) printf 'apt' ;;
                *) printf '%s' "$candidate" ;;
            esac
            return 0
        fi
    done
    nx_have brew && { printf 'brew'; return 0; }
    printf 'none'
}

# The privilege prefix for a system-wide install: "" as root, "sudo" otherwise, and a
# failure if neither is available. Homebrew is the exception and gets no prefix — brew
# refuses to run as root and will tell you so at length.
nx_sudo_prefix() {
    [ "$(id -u 2>/dev/null || echo 1000)" = "0" ] && { printf ''; return 0; }
    nx_have sudo && { printf 'sudo'; return 0; }
    return 1
}

# Background process keeping sudo credentials alive during long installations
NX_SUDO_PID=""

nx_start_sudo_keepalive() {
    [ "$(id -u 2>/dev/null || echo 1000)" = "0" ] && return 0
    nx_have sudo || return 0
    [ -n "$NX_SUDO_PID" ] && return 0
    (
        while kill -0 "$$" 2>/dev/null; do
            sudo -n true 2>/dev/null || true
            sleep 45
        done
    ) >/dev/null 2>&1 &
    NX_SUDO_PID=$!
}

nx_stop_sudo_keepalive() {
    if [ -n "${NX_SUDO_PID:-}" ]; then
        kill "$NX_SUDO_PID" 2>/dev/null || true
        wait "$NX_SUDO_PID" 2>/dev/null || true
        NX_SUDO_PID=""
    fi
}

# Ensure sudo credentials are authenticated with an interactive prompt before
# background/redirected commands run, preventing silent hangs.
nx_ensure_sudo() {
    [ "$(id -u 2>/dev/null || echo 1000)" = "0" ] && return 0
    nx_have sudo || return 1
    if sudo -n true 2>/dev/null; then
        nx_start_sudo_keepalive
        return 0
    fi
    nx_info "Administrator (sudo) privileges required. Please authenticate if prompted:"
    sudo -v || return 1
    nx_start_sudo_keepalive
    return 0
}

# --- installing -------------------------------------------------------------------------

# Concrete package name for a logical one, under a given manager. An empty answer means
# "this manager has no separate package for that" — Homebrew's python formula includes the
# venv module and the headers, so asking for them separately is an error, not a no-op.
nx_package_name() {
    local logical="$1" manager="$2"
    case "${manager}:${logical}" in
        *:git)                  printf 'git' ;;

        brew:python)            printf 'python@3.12' ;;
        apt:python)
            if nx_have apt-cache && apt-cache show python3.12 >/dev/null 2>&1; then
                printf 'python3.12'
            else
                printf 'python3'
            fi
            ;;
        pacman:python)          printf 'python' ;;
        *:python)               printf 'python3' ;;

        apt:python-venv)
            if nx_have apt-cache && apt-cache show python3.12-venv >/dev/null 2>&1; then
                printf 'python3.12-venv'
            else
                printf 'python3-venv'
            fi
            ;;
        *:python-venv)          printf '' ;;

        apt:python-dev)         printf 'python3-dev' ;;
        dnf:python-dev|yum:python-dev|zypper:python-dev) printf 'python3-devel' ;;
        apk:python-dev)         printf 'python3-dev' ;;
        *:python-dev)           printf '' ;;

        apt:build-tools)        printf 'build-essential' ;;
        dnf:build-tools|yum:build-tools) printf 'gcc gcc-c++ make' ;;
        pacman:build-tools)     printf 'base-devel' ;;
        zypper:build-tools)     printf 'gcc gcc-c++ make' ;;
        apk:build-tools)        printf 'build-base' ;;
        *:build-tools)          printf '' ;;

        brew:node)              printf 'node' ;;
        apk:node)               printf 'nodejs npm' ;;
        *:node)                 printf 'nodejs npm' ;;

        # Only where the package really is MySQL. Debian's `default-mysql-server` and
        # Arch's `mariadb` are MariaDB, and quietly installing a different database engine
        # than the schema was migrated against is not a repair.
        apt:mysql-server)       printf 'mysql-server' ;;
        dnf:mysql-server|yum:mysql-server) printf 'mysql-server' ;;
        brew:mysql-server)      printf 'mysql' ;;
        *:mysql-server)         printf '' ;;

        brew:docker)            printf 'docker docker-compose' ;;
        apt:docker)             printf 'docker.io docker-compose-v2' ;;
        dnf:docker|yum:docker)  printf 'docker docker-compose' ;;
        pacman:docker)          printf 'docker docker-compose' ;;
        apk:docker)             printf 'docker docker-cli-compose' ;;
        zypper:docker)          printf 'docker docker-compose' ;;
        *:docker)               printf '' ;;

        apt:compose-plugin)     printf 'docker-compose-plugin' ;;
        dnf:compose-plugin|yum:compose-plugin) printf 'docker-compose-plugin' ;;
        apk:compose-plugin)     printf 'docker-cli-compose' ;;
        pacman:compose-plugin|zypper:compose-plugin) printf 'docker-compose' ;;
        *:compose-plugin)       printf '' ;;

        *) printf '' ;;
    esac
}

NX_APT_UPDATED=0

# Install one or more logical packages. Returns non-zero if it could not — never dies, so
# the caller can try its next rung.
nx_install() {
    local manager="" prefix="" names="" logical="" concrete="" command_line=""

    if [ "${SETUP_AUTO_INSTALL:-1}" = "0" ]; then
        nx_warn "auto-install is off (SETUP_AUTO_INSTALL=0); not installing: $*"
        return 1
    fi

    manager="$(nx_pkg_manager)"
    if [ "$manager" = "none" ]; then
        if [ "$(nx_os)" = "macos" ]; then
            nx_warn "no Homebrew on this Mac, so packages cannot be installed automatically."
            nx_info "Install it once with the command at https://brew.sh and re-run, or install: $*"
        else
            nx_warn "no supported package manager found (apt/dnf/yum/pacman/zypper/apk)."
        fi
        return 1
    fi

    for logical in "$@"; do
        concrete="$(nx_package_name "$logical" "$manager")"
        [ -n "$concrete" ] && names="${names} ${concrete}"
    done
    # Everything asked for is bundled into something else under this manager.
    [ -n "$names" ] || return 0

    if [ "$manager" = "brew" ]; then
        prefix=""
    elif ! prefix="$(nx_sudo_prefix)"; then
        nx_warn "root is needed to install${names}, and neither sudo nor a root shell is available."
        return 1
    else
        nx_ensure_sudo || return 1
    fi

    case "$manager" in
        apt)    command_line="DEBIAN_FRONTEND=noninteractive ${prefix} apt-get install -y --no-install-recommends${names}" ;;
        dnf)    command_line="${prefix} dnf install -y${names}" ;;
        yum)    command_line="${prefix} yum install -y${names}" ;;
        pacman) command_line="${prefix} pacman -S --noconfirm --needed${names}" ;;
        zypper) command_line="${prefix} zypper --non-interactive install${names}" ;;
        apk)    command_line="${prefix} apk add --no-cache${names}" ;;
        brew)   command_line="brew install${names}" ;;
    esac

    nx_fix "installing:${names}"
    nx_dim "$command_line"
    nx_confirm "run it?" || { nx_warn "skipped"; return 1; }

    # apt's package lists on a long-lived machine are usually stale enough that install
    # 404s on a moved version. Refresh once per run, not once per package.
    if [ "$manager" = "apt" ] && [ "$NX_APT_UPDATED" = "0" ]; then
        NX_APT_UPDATED=1
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive $prefix apt-get update >/dev/null 2>&1 || nx_warn "apt-get update failed; trying the install anyway"
    fi

    # shellcheck disable=SC2086
    if $command_line >/dev/null 2>&1; then
        nx_ok "installed:${names}"
        return 0
    fi
    # Second time with the output visible: whatever apt or dnf has to say about a held
    # package or a missing repository is more useful than "install failed".
    nx_warn "install failed; repeating it with output so the reason is visible"
    # shellcheck disable=SC2086
    $command_line && return 0
    return 1
}

# Make sure CMD exists, installing LOGICAL_PACKAGE if it does not.
nx_ensure_command() {
    local command_name="$1" logical="${2:-$1}"
    nx_have "$command_name" && return 0
    nx_warn "${command_name} is not installed"
    nx_install "$logical" || return 1
    nx_have "$command_name"
}

# --- python -----------------------------------------------------------------------------

# The newest interpreter on this machine that is at least MIN, or "" if there is none.
#
# Ordered newest-first and not simply `python3`, because a distribution's default python3
# is routinely years behind an interpreter installed beside it: Ubuntu 22.04's is 3.10,
# with a 3.12 added later reachable only as `python3.12`. Taking `python3` there gets an
# interpreter that cannot even parse the app (PYTHON_MINIMUM in bootstrap.sh says why).
nx_find_python() {
    local minimum="${1:-3.12}" candidate="" version=""
    for candidate in python3.13 python3.12 python3 python; do
        nx_have "$candidate" || continue
        version="$(nx_version_of "$candidate" -V)"
        [ -n "$version" ] || continue
        if nx_version_at_least "$version" "$minimum"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

# --- node -------------------------------------------------------------------------------

# Node at least MIN, trying a version manager before giving up. Version managers are
# checked because a machine that has one has almost certainly got the right Node installed
# already under it — just not on this shell's PATH.
#
# Answers by exit status only and prints nothing to stdout, so there is no reason to call
# it inside `$(...)` — which would run the fnm/nvm PATH repair in a subshell and lose it.
nx_find_node() {
    local minimum="${1:-24}" version=""
    if nx_have node; then
        version="$(nx_version_of node -v)"
        if [ -n "$version" ] && nx_version_at_least "$version" "$minimum"; then
            return 0
        fi
    fi

    if nx_have fnm; then
        nx_fix "asking fnm for Node ${minimum}"
        fnm install "$minimum" >/dev/null 2>&1 || true
        eval "$(fnm env 2>/dev/null)" || true
        fnm use "$minimum" >/dev/null 2>&1 || true
    elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
        nx_fix "asking nvm for Node ${minimum}"
        # nvm.sh is not written for `set -u`, and this runs in setup's own shell (so that the
        # PATH it sets survives), where an unbound variable inside nvm would end the run.
        local nounset=""
        case $- in *u*) nounset=1; set +u ;; esac
        # shellcheck disable=SC1091
        . "${NVM_DIR:-$HOME/.nvm}/nvm.sh" || true
        nvm install "$minimum" >/dev/null 2>&1 || true
        nvm use "$minimum" >/dev/null 2>&1 || true
        if [ -n "$nounset" ]; then set -u; fi
    fi

    if nx_have node; then
        version="$(nx_version_of node -v)"
        if [ -n "$version" ] && nx_version_at_least "$version" "$minimum"; then
            return 0
        fi
    fi
    return 1
}

# --- docker -----------------------------------------------------------------------------

# Set by nx_docker_ready: the docker invocation that works on this machine. It is a
# *string* holding one or two words ("docker", or "sudo docker" when the operator is not
# yet in the docker group), so every call site has to leave it unquoted.
NX_DOCKER="docker"
NX_COMPOSE="docker compose"

nx_start_docker_daemon() {
    local prefix=""

    case "$(nx_os)" in
        macos)
            # Docker Desktop is a GUI application; `open -a` is the supported way to start
            # it, and it takes a while to be ready after the window appears.
            if [ -d "/Applications/Docker.app" ]; then
                nx_fix "starting Docker Desktop"
                open -a Docker >/dev/null 2>&1 || return 1
                return 0
            fi
            if nx_have colima; then
                nx_fix "starting colima"
                colima start >/dev/null 2>&1 && return 0
            fi
            return 1
            ;;
        windows)
            if [ -x "/c/Program Files/Docker/Docker/Docker Desktop.exe" ]; then
                nx_fix "starting Docker Desktop"
                "/c/Program Files/Docker/Docker/Docker Desktop.exe" >/dev/null 2>&1 &
                return 0
            fi
            return 1
            ;;
        linux)
            prefix="$(nx_sudo_prefix)" || return 1
            nx_ensure_sudo || return 1
            if nx_have systemctl; then
                nx_fix "starting the Docker service"
                # shellcheck disable=SC2086
                $prefix systemctl start docker >/dev/null 2>&1 && return 0
                return 1
            fi
            if nx_have service; then
                nx_fix "starting the Docker service"
                # shellcheck disable=SC2086
                $prefix service docker start >/dev/null 2>&1 && return 0
            fi
            return 1
            ;;
    esac
    return 1
}

# Install Docker and Docker Compose on the host operating system.
nx_install_docker() {
    local manager="" prefix=""
    manager="$(nx_pkg_manager)"
    nx_fix "installing Docker and Docker Compose for $(nx_os) via ${manager}"

    if [ "$manager" = "brew" ]; then
        if brew install --cask docker; then
            nx_ok "Docker Desktop installed via Homebrew"
            nx_start_docker_daemon || true
            return 0
        elif brew install docker docker-compose colima; then
            nx_ok "Docker CLI and Colima installed via Homebrew"
            colima start >/dev/null 2>&1 || true
            return 0
        fi
        return 1
    fi

    if [ "$(nx_os)" = "windows" ]; then
        if nx_have winget; then
            nx_fix "winget install --id Docker.DockerDesktop"
            winget install --id Docker.DockerDesktop --silent --accept-package-agreements --accept-source-agreements || return 1
            nx_ok "Docker Desktop installed"
            return 0
        fi
        return 1
    fi

    # Linux package managers
    if ! nx_install docker; then
        if [ "$manager" = "apt" ]; then
            nx_ensure_sudo || return 1
            prefix="$(nx_sudo_prefix)"
            # shellcheck disable=SC2086
            DEBIAN_FRONTEND=noninteractive $prefix apt-get update >/dev/null 2>&1 || true
            # shellcheck disable=SC2086
            DEBIAN_FRONTEND=noninteractive $prefix apt-get install -y --no-install-recommends docker.io docker-compose-plugin >/dev/null 2>&1 \
                || DEBIAN_FRONTEND=noninteractive $prefix apt-get install -y --no-install-recommends docker.io docker-compose >/dev/null 2>&1 \
                || return 1
        else
            return 1
        fi
    fi

    if [ "$(nx_os)" = "linux" ]; then
        nx_start_docker_daemon || true
        if [ "$(id -u 2>/dev/null || echo 1000)" != "0" ] && [ -n "${USER:-}" ]; then
            nx_ensure_sudo || true
            prefix="$(nx_sudo_prefix)"
            # shellcheck disable=SC2086
            $prefix usermod -aG docker "$USER" 2>/dev/null || true
        fi
    fi
    return 0
}

# Can this machine run the stack in Docker? Sets NX_DOCKER and NX_COMPOSE on success.
#
# The failures below are handled with distinct repairs:
#   * the CLI is absent            → offer to install Docker, or fall back to native
#   * the daemon is not running    → start it and wait
#   * the socket refuses this user → run through sudo now, and offer the group fix for later
nx_docker_ready() {
    local probe="" waited=0

    if ! nx_have docker; then
        if [ "${MODE:-auto}" = "docker" ] || [ "${MODE:-auto}" = "auto" ]; then
            if [ "${SETUP_AUTO_INSTALL:-1}" != "0" ]; then
                if nx_confirm "Docker is not installed. Would you like setup to install Docker and Docker Compose?"; then
                    nx_install_docker || return 1
                else
                    return 1
                fi
            else
                return 1
            fi
        else
            return 1
        fi
    fi

    if docker info >/dev/null 2>&1; then
        NX_DOCKER="docker"
        nx_compose_ready
        return $?
    fi

    probe="$(docker info 2>&1 || true)"

    case "$probe" in
        *"permission denied"*|*"Got permission denied"*|*"connect: permission denied"*)
            nx_warn "the Docker socket refuses this user — you are not in the 'docker' group"
            if nx_have sudo; then
                nx_ensure_sudo || true
                if sudo docker info >/dev/null 2>&1; then
                    NX_DOCKER="sudo docker"
                    nx_fix "using 'sudo docker' for this run"
                    nx_info "To stop needing sudo:  sudo usermod -aG docker \"\$USER\"  then log out and back in."
                    nx_compose_ready
                    return $?
                fi
            fi
            return 1
            ;;
    esac

    # Anything else is the daemon not being up. Try to start it, then wait.
    nx_warn "the Docker daemon is not responding"
    nx_start_docker_daemon || return 1

    while [ "$waited" -lt 60 ]; do
        if docker info >/dev/null 2>&1; then
            nx_ok "Docker is up"
            NX_DOCKER="docker"
            nx_compose_ready
            return $?
        fi
        sleep 3
        waited=$((waited + 3))
        [ $((waited % 15)) -eq 0 ] && nx_dim "still waiting for Docker (${waited}s / 60s)"
    done

    nx_warn "Docker did not come up within 60 seconds"
    return 1
}

# Read the database credentials back out of an existing container.
#
# This is the repair for the worst .env accident there is: a checkout whose MYSQL_* keys
# have gone missing — restored from a template, replaced by a teammate's copy, or simply
# never written because the stack was first started some other way — while the database
# volume they created is still there and still full of the operator's data.
#
# MySQL bakes MYSQL_USER/MYSQL_PASSWORD into the data directory when it initialises and
# ignores the environment ever after, so a regenerated password can never open that volume
# again. But the container was *given* those values, and Docker keeps a container's
# environment in its config — for a stopped container as much as a running one. So the only
# surviving copy of the password is one `docker inspect` away, and putting it back in .env
# restores the stack exactly as it was.
#
# Only ever fills in keys that have no value. A key the operator has set is never touched,
# because a deliberate change of password is indistinguishable from a wrong one here.
nx_recover_db_credentials() {
    local project="$1" file="$2" container="" env_dump="" line="" key="" recovered=""

    nx_have docker || return 1
    $NX_DOCKER info >/dev/null 2>&1 || return 1

    # -aq, not -q: a stopped container is the more likely case, and it holds the same
    # config. This is exactly when the operator needs the values most.
    container="$($NX_DOCKER ps -aq \
        --filter "label=com.docker.compose.project=${project}" \
        --filter "label=com.docker.compose.service=db" 2>/dev/null | head -1 || true)"
    [ -n "$container" ] || return 1

    env_dump="$($NX_DOCKER inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" 2>/dev/null || true)"
    [ -n "$env_dump" ] || return 1

    for key in MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD MYSQL_ROOT_PASSWORD; do
        [ -z "$(nx_env_get "$key" "$file")" ] || continue
        line="$(printf '%s\n' "$env_dump" | grep -E "^${key}=." | head -1 || true)"
        [ -n "$line" ] || continue
        nx_env_write "$key" "${line#*=}" "$file"
        recovered="${recovered} ${key}"
    done

    [ -n "$recovered" ] || return 1
    nx_fix "recovered${recovered} from the existing '${project}-db' container"
    nx_info "  That volume keeps working. A regenerated password never could have opened it —"
    nx_info "  MySQL stores the credentials inside the data directory when it first initialises."
    return 0
}

# Compose v2 is a docker CLI plugin; v1 was a separate `docker-compose` binary. Both are
# still in the wild, and the v1 fallback is worth having because the compose file here uses
# nothing v2-specific except `depends_on.condition`, which v1.29 also supports.
nx_compose_ready() {
    if $NX_DOCKER compose version >/dev/null 2>&1; then
        NX_COMPOSE="$NX_DOCKER compose"
        return 0
    fi
    nx_warn "the Docker Compose v2 plugin is missing"
    if nx_have docker-compose; then
        nx_fix "falling back to the standalone docker-compose"
        NX_COMPOSE="docker-compose"
        return 0
    fi
    if nx_install compose-plugin && $NX_DOCKER compose version >/dev/null 2>&1; then
        NX_COMPOSE="$NX_DOCKER compose"
        return 0
    fi
    return 1
}

# --- pre-flight plan & upfront permission gathering -------------------------------------
#
# Inspect all prerequisites, toolchains and system permissions before step 1 so all
# confirmations, decisions and administrator permissions are gathered once upfront,
# allowing the rest of setup to run completely unattended.
nx_preflight_plan() {
    local missing_logical=""
    local missing_concrete=""
    local need_sudo=0
    local manager=""
    local need_docker_start=0
    local need_docker_group=0
    local need_mysql_create=0

    # If diagnosis only or auto-install disabled, skip upfront interactive prompting
    if [ "${CHECK_ONLY:-0}" = "1" ] || [ "${SETUP_AUTO_INSTALL:-1}" = "0" ]; then
        return 0
    fi

    manager="$(nx_pkg_manager)"

    # 1. Check Git: needed to fetch the SPA if missing
    if [ ! -f "$APP_DIR/package.json" ]; then
        if ! nx_have git; then
            missing_logical="${missing_logical} git"
        fi
    fi

    # 2. Check Toolchain (Docker vs Native)
    if [ "$MODE" = "docker" ] || [ "$MODE" = "auto" ]; then
        if ! nx_have docker; then
            if [ "$MODE" = "docker" ]; then
                missing_logical="${missing_logical} docker compose-plugin"
            elif [ "$manager" != "brew" ] && [ "$(id -u 2>/dev/null || echo 1000)" != "0" ] && ! nx_have sudo; then
                nx_warn "Docker is not installed and administrator (sudo) privileges are not available to install it."
                nx_fix "falling back to a native install (Python venv + npm)"
                MODE="native"
            else
                # Auto mode: ask upfront about installing Docker or falling back to native
                if [ "${SETUP_ASSUME_YES:-0}" = "1" ] || [ ! -t 0 ]; then
                    missing_logical="${missing_logical} docker compose-plugin"
                else
                    nx_warn "Docker is not installed on this machine."
                    if nx_confirm "Would you like setup to install Docker and Docker Compose? (n = fall back to native Python+Node+MySQL)"; then
                        missing_logical="${missing_logical} docker compose-plugin"
                    else
                        nx_fix "falling back to native install (Python venv + npm + MySQL)"
                        MODE="native"
                    fi
                fi
            fi
        else
            # Docker CLI is present. Check if daemon is responsive
            if docker info >/dev/null 2>&1; then
                : # Docker daemon is running and accessible
            else
                local probe=""
                probe="$(docker info 2>&1 || true)"
                case "$probe" in
                    *"permission denied"*|*"Got permission denied"*|*"connect: permission denied"*)
                        if [ "$(id -u 2>/dev/null || echo 1000)" != "0" ]; then
                            need_docker_group=1
                            need_sudo=1
                        fi
                        ;;
                    *)
                        need_docker_start=1
                        if [ "$(nx_os)" = "linux" ]; then
                            need_sudo=1
                        fi
                        ;;
                esac
            fi

            # Check Compose availability
            if ! docker compose version >/dev/null 2>&1 && ! nx_have docker-compose; then
                missing_logical="${missing_logical} compose-plugin"
            fi
        fi
    fi

    # 3. Check Native dependencies if MODE is native (requested or fell back)
    if [ "$MODE" = "native" ]; then
        # Check Python
        local py_cand=""
        if ! py_cand="$(nx_find_python "${PYTHON_MINIMUM:-3.12}" 2>/dev/null || true)"; then
            missing_logical="${missing_logical} python python-venv"
        else
            if ! "$py_cand" -c 'import venv, ensurepip' >/dev/null 2>&1; then
                missing_logical="${missing_logical} python-venv"
            fi
        fi

        # Check Node
        if ! nx_find_node "${NODE_MINIMUM:-24}" >/dev/null 2>&1; then
            missing_logical="${missing_logical} node"
        fi

        # Check Database
        # Only needed if Docker is not active to run the DB container
        if ! docker info >/dev/null 2>&1; then
            if ! nx_have mysqld && ! nx_have mysql; then
                missing_logical="${missing_logical} mysql-server"
            else
                need_mysql_create=1
            fi
        fi
    fi

    # 4. Resolve package names and check if sudo is needed for installation
    local item="" concrete=""
    for item in $missing_logical; do
        concrete="$(nx_package_name "$item" "$manager")"
        if [ -n "$concrete" ]; then
            case " $missing_concrete " in
                *" $concrete "*) ;;
                *) missing_concrete="${missing_concrete} ${concrete}" ;;
            esac
            if [ "$manager" != "brew" ] && [ "$(id -u 2>/dev/null || echo 1000)" != "0" ]; then
                need_sudo=1
            fi
        fi
    done

    # Trim leading space
    missing_concrete="${missing_concrete# }"

    # If nothing missing and no elevated actions required, proceed seamlessly
    if [ -z "$missing_concrete" ] && [ "$need_sudo" -eq 0 ] && [ "$need_docker_start" -eq 0 ] && [ "$need_mysql_create" -eq 0 ]; then
        return 0
    fi

    # 5. Display unified pre-flight summary
    nx_step "Pre-flight verification & permissions"
    if [ -n "$missing_concrete" ]; then
        nx_info "Prerequisites to install: ${missing_concrete} (via ${manager})"
    fi
    if [ "$need_docker_start" -eq 1 ]; then
        nx_info "System action: start Docker service"
    fi
    if [ "$need_docker_group" -eq 1 ]; then
        nx_info "System action: configure Docker socket permissions for user"
    fi
    if [ "$need_mysql_create" -eq 1 ]; then
        nx_info "System action: configure database on local MySQL server"
    fi
    if [ "$need_sudo" -eq 1 ]; then
        nx_info "Permissions required: Administrator (sudo) access"
    fi

    # 6. Request upfront confirmation
    if [ "${SETUP_ASSUME_YES:-0}" != "1" ] && [ -t 0 ]; then
        if ! nx_confirm "Proceed with all installations and setup?"; then
            nx_die "Setup aborted by user."
        fi
        # Once confirmed upfront, assume yes for all subsequent commands in this run
        SETUP_ASSUME_YES=1
        export SETUP_ASSUME_YES
    fi

    # 7. Authenticate sudo upfront and start background keep-alive
    if [ "$need_sudo" -eq 1 ]; then
        if ! nx_ensure_sudo; then
            if [ "$MODE" = "docker" ]; then
                nx_die "Docker was requested with --docker, but administrator (sudo) privileges could not be authenticated.
       Please have an administrator install Docker, or use native mode:
       make setup-native"
            else
                nx_warn "Administrator (sudo) privileges could not be authenticated. Cannot install system packages."
                nx_fix "falling back to a native install (Python venv + npm)"
                MODE="native"
                return 0
            fi
        fi
        nx_ok "Administrator privileges authenticated"
    fi
}

