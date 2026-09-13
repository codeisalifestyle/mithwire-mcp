#!/usr/bin/env bash
# =============================================================================
# Run MithWire Stealth Tests Inside Docker (Linux Server Environment)
#
# Exercises both pure CDP engine and CloakBrowser Stealth engine across native
# and cross-OS spoofing profiles (Linux, Windows, Mac) inside a Linux container.
#
# Usage:
#   ./scripts/run_docker_tests.sh                          # Default: verify + matrix
#   ./scripts/run_docker_tests.sh --verify                 # Run verify_mcp test suite
#   ./scripts/run_docker_tests.sh --matrix                 # Run profile matrix
#   ./scripts/run_docker_tests.sh --engine cdp             # CDP engine only
#   ./scripts/run_docker_tests.sh --engine stealth         # Stealth engine only
#   ./scripts/run_docker_tests.sh --profiles 'win-*'       # Cross-OS Windows profiles
#   ./scripts/run_docker_tests.sh --profiles 'linux-*'     # Native Linux profiles
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="mithwire-mcp:test"
CACHE_DIR="${HOME}/.cloakbrowser"

mkdir -p "${CACHE_DIR}"

MODE="all"
ENGINE="both"
PROFILES="linux-us*,win-us*"
BUILD_IMAGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD_IMAGE=1
      shift
      ;;
    --verify)
      MODE="verify"
      shift
      ;;
    --matrix)
      MODE="matrix"
      shift
      ;;
    --engine)
      ENGINE="$2"
      shift 2
      ;;
    --profiles)
      PROFILES="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--build] [--verify] [--matrix] [--engine cdp|stealth|both] [--profiles '<glob>']"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Ensure image exists or rebuild
if [[ "${BUILD_IMAGE}" -eq 1 ]] || ! docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
  echo "==> Building ${IMAGE_NAME} on platform linux/amd64..."
  docker build --platform linux/amd64 -t "${IMAGE_NAME}" "${REPO_ROOT}"
fi

run_in_docker() {
  docker run --rm \
    --platform linux/amd64 \
    --security-opt seccomp=unconfined \
    -v "${REPO_ROOT}:/app" \
    -v "${CACHE_DIR}:/home/mithwire/.cloakbrowser" \
    -w /app \
    --entrypoint python \
    "${IMAGE_NAME}" "$@"
}

ENGINES=()
if [[ "${ENGINE}" == "both" ]]; then
  ENGINES=("cdp" "stealth")
elif [[ "${ENGINE}" == "cdp" ]]; then
  ENGINES=("cdp")
elif [[ "${ENGINE}" == "stealth" ]]; then
  ENGINES=("stealth")
else
  echo "Invalid engine: ${ENGINE} (choose cdp, stealth, or both)"
  exit 1
fi

echo "======================================================================="
echo "MithWire Dockerized Test Suite (Linux x86_64 container)"
echo "Mode: ${MODE} | Engines: ${ENGINES[*]} | Profiles: ${PROFILES}"
echo "======================================================================="

FAILED=0

if [[ "${MODE}" == "all" || "${MODE}" == "verify" ]]; then
  for eng in "${ENGINES[@]}"; do
    echo ""
    echo "-----------------------------------------------------------------------"
    echo "Running verify_mcp in Docker (Engine: ${eng})"
    echo "-----------------------------------------------------------------------"
    for site in sannysoft rebrowser browserscan incolumitas deviceinfo; do
      echo ">> Testing site: ${site} [engine=${eng}]"
      if ! run_in_docker scripts/verify_mcp.py --headless --engine "${eng}" --site "${site}"; then
        echo "FAILED: verify_mcp site ${site} on engine ${eng}"
        FAILED=1
      fi
    done
  done
fi

if [[ "${MODE}" == "all" || "${MODE}" == "matrix" ]]; then
  for eng in "${ENGINES[@]}"; do
    echo ""
    echo "-----------------------------------------------------------------------"
    echo "Running Profile Matrix in Docker (Engine: ${eng}, Profiles: ${PROFILES})"
    echo "-----------------------------------------------------------------------"
    if ! run_in_docker scripts/profile_matrix.py \
        --driver bridge \
        --headless \
        --engine "${eng}" \
        --profiles-filter "${PROFILES}" \
        --skip-fpcom \
        --skip-browserleaks \
        --skip-captcha \
        --skip-detection \
        --skip-ipquality; then
      echo "FAILED: Profile Matrix on engine ${eng} with profiles ${PROFILES}"
      FAILED=1
    fi
  done
fi

echo ""
echo "======================================================================="
if [[ "${FAILED}" -eq 0 ]]; then
  echo "ALL DOCKER TESTS PASSED SUCCESSFULLY!"
  echo "======================================================================="
  exit 0
else
  echo "SOME DOCKER TESTS FAILED!"
  echo "======================================================================="
  exit 1
fi
