#!/usr/bin/env bash
#
# DALI Load Test Runner
#
# This script sets up and runs load tests against DALI. It handles:
#   1. Generating test students
#   2. Starting DALI with the test roster
#   3. Running Locust with various configurations
#
# Usage:
#   ./run_load_test.sh setup          Generate test students
#   ./run_load_test.sh tls-test       Reproduce the TLS worker starvation bug
#   ./run_load_test.sh quick          Quick smoke test (10 users, 2 min)
#   ./run_load_test.sh classroom      Simulate classroom scenario (50 users, 5 min)
#   ./run_load_test.sh stress         Full stress test (100 users, 10 min)
#   ./run_load_test.sh interactive    Open Locust web UI for manual control

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROSTER="$SCRIPT_DIR/test_students.csv"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }

# -------------------------------------------------------------------------
# Preflight checks
# -------------------------------------------------------------------------

check_dependencies() {
    local missing=()
    command -v python3 >/dev/null || missing+=(python3)
    python3 -c "import locust" 2>/dev/null || missing+=(locust)
    
    if [ ${#missing[@]} -gt 0 ]; then
        error "Missing dependencies: ${missing[*]}"
        echo "  Install with: pip install locust"
        exit 1
    fi
}

check_roster() {
    if [ ! -f "$ROSTER" ]; then
        warn "Test roster not found. Generating..."
        python3 "$SCRIPT_DIR/generate_test_students.py" --output "$ROSTER"
    fi
    local count
    count=$(tail -n +2 "$ROSTER" | wc -l)
    ok "Test roster: $ROSTER ($count students)"
}

check_server() {
    local host="${1:-http://localhost:5000}"
    info "Checking if DALI is running at $host..."
    
    if curl -sk --max-time 5 "$host/health" >/dev/null 2>&1; then
        ok "DALI is running"
        # Show health info
        curl -sk "$host/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || true
        echo
    else
        error "Cannot reach DALI at $host"
        echo
        echo "Start DALI with the test roster:"
        echo
        echo "  # For HTTP (recommended for testing):"
        echo "  ROSTER_CSV_PATH=$ROSTER python3 app_complete.py"
        echo
        echo "  # For HTTPS (to test TLS issues):"
        echo "  ROSTER_CSV_PATH=$ROSTER gunicorn app_complete:app \\"
        echo "      --certfile cert.pem --keyfile key.pem \\"
        echo "      --workers 2 --timeout 30 --bind 0.0.0.0:5000"
        echo
        exit 1
    fi
}

# -------------------------------------------------------------------------
# Test scenarios
# -------------------------------------------------------------------------

cmd_setup() {
    info "Generating 100 test students..."
    python3 "$SCRIPT_DIR/generate_test_students.py" --count 100 --output "$ROSTER"
    echo
    ok "Setup complete."
    echo
    echo "Next steps:"
    echo "  1. Start DALI with: ROSTER_CSV_PATH=$ROSTER python3 app_complete.py"
    echo "  2. Run a test:      $0 quick"
}

cmd_tls_test() {
    echo
    info "============================================="
    info " TLS Worker Starvation Test"
    info "============================================="
    echo
    echo "This test reproduces the classroom issue: failed TLS handshakes"
    echo "blocking gunicorn sync workers so legitimate requests time out."
    echo
    echo "INSTRUCTIONS:"
    echo
    echo "  1. Start DALI with sync workers + self-signed cert:"
    echo
    echo "     ROSTER_CSV_PATH=$ROSTER gunicorn app_complete:app \\"
    echo "         --certfile cert.pem --keyfile key.pem \\"
    echo "         --workers 2 --timeout 30 --bind 0.0.0.0:5000"
    echo
    echo "  2. In another terminal, run this test:"
    echo
    echo "     locust -f $SCRIPT_DIR/locustfile.py \\"
    echo "         --host https://localhost:5000 \\"
    echo "         --users 10 --spawn-rate 5 --run-time 2m --headless"
    echo
    echo "  3. Watch gunicorn logs — you should see WORKER TIMEOUT errors"
    echo "     and many requests will fail or be very slow."
    echo
    echo "  4. Now restart DALI with gevent workers:"
    echo
    echo "     ROSTER_CSV_PATH=$ROSTER gunicorn app_complete:app \\"
    echo "         --certfile cert.pem --keyfile key.pem \\"
    echo "         --workers 2 --worker-class gevent \\"
    echo "         --worker-connections 100 \\"
    echo "         --timeout 30 --bind 0.0.0.0:5000"
    echo
    echo "  5. Re-run the same Locust test. Requests should succeed."
    echo
}

cmd_quick() {
    local host="${1:-http://localhost:5000}"
    check_dependencies
    check_roster
    check_server "$host"
    
    info "Quick smoke test: 10 users, 2 minutes"
    info "No TLS abuse (testing basic workflow)"
    echo
    
    SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV="$ROSTER" \
    locust -f "$SCRIPT_DIR/locustfile.py" \
        --host "$host" \
        --users 10 \
        --spawn-rate 2 \
        --run-time 2m \
        --headless \
        --only-summary
}

cmd_classroom() {
    local host="${1:-http://localhost:5000}"
    check_dependencies
    check_roster
    check_server "$host"
    
    info "Classroom simulation: 50 users, 5 minutes"
    info "Students arrive over 60 seconds (realistic ramp-up)"
    echo
    
    SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV="$ROSTER" \
    locust -f "$SCRIPT_DIR/locustfile.py" \
        --host "$host" \
        --users 50 \
        --spawn-rate 1 \
        --run-time 5m \
        --headless \
        --csv="$SCRIPT_DIR/results/classroom" \
        --html="$SCRIPT_DIR/results/classroom_report.html"
    
    echo
    ok "Results saved to $SCRIPT_DIR/results/"
    echo "  CSV:  results/classroom_stats.csv"
    echo "  HTML: results/classroom_report.html"
}

cmd_stress() {
    local host="${1:-http://localhost:5000}"
    check_dependencies
    check_roster
    check_server "$host"
    
    info "Stress test: 100 users, 10 minutes"
    warn "This will generate significant load!"
    echo
    
    SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV="$ROSTER" \
    locust -f "$SCRIPT_DIR/locustfile.py" \
        --host "$host" \
        --users 100 \
        --spawn-rate 2 \
        --run-time 10m \
        --headless \
        --csv="$SCRIPT_DIR/results/stress" \
        --html="$SCRIPT_DIR/results/stress_report.html"
    
    echo
    ok "Results saved to $SCRIPT_DIR/results/"
}

cmd_interactive() {
    local host="${1:-http://localhost:5000}"
    check_dependencies
    check_roster
    check_server "$host"
    
    info "Starting Locust web UI..."
    info "Open http://localhost:8089 in your browser"
    info "Set host to: $host"
    echo
    
    TEST_ROSTER_CSV="$ROSTER" \
    locust -f "$SCRIPT_DIR/locustfile.py" \
        --host "$host"
}

# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

mkdir -p "$SCRIPT_DIR/results"

case "${1:-help}" in
    setup)
        cmd_setup
        ;;
    tls-test)
        cmd_tls_test
        ;;
    quick)
        cmd_quick "${2:-http://localhost:5000}"
        ;;
    classroom)
        cmd_classroom "${2:-http://localhost:5000}"
        ;;
    stress)
        cmd_stress "${2:-http://localhost:5000}"
        ;;
    interactive)
        cmd_interactive "${2:-http://localhost:5000}"
        ;;
    help|--help|-h|*)
        echo "DALI Load Test Runner"
        echo
        echo "Usage: $0 <command> [host]"
        echo
        echo "Commands:"
        echo "  setup          Generate 100 test students"
        echo "  tls-test       Print instructions for TLS starvation test"
        echo "  quick [host]   10 users, 2 min (default: http://localhost:5000)"
        echo "  classroom      50 users, 5 min, saves CSV + HTML report"
        echo "  stress         100 users, 10 min, saves CSV + HTML report"
        echo "  interactive    Open Locust web UI for manual control"
        echo
        echo "Examples:"
        echo "  $0 setup"
        echo "  $0 quick"
        echo "  $0 classroom https://localhost:5000"
        echo "  $0 interactive http://dali.rice.edu:5000"
        echo
        echo "Environment variables:"
        echo "  ASSIGNMENT_ID    Assignment to test (default: 505415)"
        echo "  LAB_NAME         Lab directory name (default: lab3)"
        echo "  TLS_FAILURES     Failed handshakes per user (default: 2)"
        ;;
esac
