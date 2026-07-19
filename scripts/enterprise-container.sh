#!/usr/bin/env bash
# Enterprise test container management for iris-vector-graph (Spec 190).
# Manages ivg-iris-enterprise (Enterprise IRIS + libarno_callout.so).
# Paired with scripts/test-container.sh (Community ivg-iris).
#
# Usage:
#   scripts/enterprise-container.sh up        # Start ivg-iris-enterprise, deploy, init, load arno
#   scripts/enterprise-container.sh down      # Stop and remove ivg-iris-enterprise
#   scripts/enterprise-container.sh status    # Check health + arno loaded
#   scripts/enterprise-container.sh deploy    # Deploy iris_src/src/ to /tmp/src in container
#
# Constitution Principle IV grounding:
#   Container: ivg-iris-enterprise  (Registry: iris-vector-graph-enterprise entry)
#   Port:      31972 host → 1972 container
#   .so:       docker/enterprise/libarno_callout.so → /tmp/libarno_callout.so inside container
#   Verified against docker/enterprise/docker-compose.yml and lab_manager registry.

set -euo pipefail

CONTAINER="${IVG_ARNO_CONTAINER:-ivg-iris-enterprise}"
COMPOSE_FILE="docker/enterprise/docker-compose.yml"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cmd="${1:-status}"

case "$cmd" in
  up)
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
      echo "✓ $CONTAINER already running"
      exit 0
    fi
    echo "Starting $CONTAINER via docker compose..."
    docker compose -f "$REPO_ROOT/$COMPOSE_FILE" up -d
    echo "Waiting for IRIS to be ready (up to 3 min)..."
    for i in $(seq 1 36); do
      if docker ps --filter "name=$CONTAINER" --filter "health=healthy" --format '{{.Names}}' | grep -qx "$CONTAINER" 2>/dev/null; then
        echo "  container healthy after ${i}×5s"
        break
      fi
      sleep 5
    done
    "$0" deploy
    echo "Initializing schema..."
    python3 -c "
import subprocess, iris
ip = subprocess.run(['docker','inspect','$CONTAINER','--format',
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'],
    capture_output=True, text=True).stdout.strip()
conn = None
if ip:
    try:
        conn = iris.connect(hostname=ip, port=1972, namespace='USER', username='_SYSTEM', password='SYS')
    except Exception:
        conn = None
if conn is None:
    # Container IP not routable from host (macOS Docker Desktop/OrbStack) —
    # fall back to the published port on localhost, same as tests/conftest.py.
    try:
        conn = iris.connect(hostname='localhost', port=31972, namespace='USER', username='_SYSTEM', password='SYS')
    except Exception:
        from iris_devtester import IRISContainer as C
        c = C.attach('$CONTAINER'); c._connection = None; conn = c.get_connection()
from iris_vector_graph import IRISGraphEngine
# 768 matches schema.py's own default (get_base_schema_sql) and what most of
# the e2e/integration suite assumes for kg_NodeEmbeddings' VECTOR dimension.
IRISGraphEngine(conn, embedding_dimension=768).initialize_schema()
print('✓ schema initialized')
" 2>&1 | grep -E 'schema initialized|ERROR|CRITICAL' | grep -v 'Embedding dimension'
    echo "Deploying and compiling ObjectScript via TCP..."
    # The HealthShare enterprise image has a dual-database split: iris session -U USER
    # routes ^%Dictionary* globals to IRISSYS while TCP connections use the USER IRIS.DAT.
    # Classes must be deployed via TCP to be visible to test connections on port 31972.
    "$0" tcp-deploy 2>&1 | grep -iE 'ERROR|deployed|failed'
    echo "Loading libarno_callout.so via TCP..."
    "$0" tcp-load-arno 2>&1 | grep -iE 'ERROR|loaded|failed'
    "$(dirname "$0")/install-embedded-deps.sh" "$CONTAINER" || true
    echo "✓ $CONTAINER ready (Enterprise + Arno)"
    ;;

  down)
    echo "Stopping $CONTAINER..."
    docker compose -f "$REPO_ROOT/$COMPOSE_FILE" down
    echo "✓ $CONTAINER stopped"
    ;;

  status)
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
      echo "$CONTAINER	$(docker ps --filter "name=$CONTAINER" --format '{{.Status}}')"
      python3 -c "
import subprocess, iris, json
ip = subprocess.run(['docker','inspect','$CONTAINER','--format',
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'],
    capture_output=True, text=True).stdout.strip()
try:
    if ip:
        conn = iris.connect(hostname=ip, port=1972, namespace='USER', username='_SYSTEM', password='SYS')
    else:
        from iris_devtester import IRISContainer as C
        c = C.attach('$CONTAINER'); c._connection = None; conn = c.get_connection()
    irisobj = iris.createIRIS(conn)
    # Load arno to get accurate rust_callout status
    try: irisobj.classMethodValue('Graph.KG.ArnoAccel', 'Load', '/tmp/libarno_callout.so')
    except: pass
    caps = irisobj.classMethodValue('Graph.KG.NKGAccel','Capabilities')
    d = json.loads(str(caps))
    print('  arno rust_callout:', d.get('rust_callout', False))
    print('  arno bfs:', d.get('bfs', False))
except Exception as e:
    print('  arno status: unavailable -', str(e)[:60])
" 2>&1 | grep -vE 'swigvarlink|IVG setup|Deprecat'
    else
      echo "$CONTAINER not running (start with: scripts/enterprise-container.sh up)"
      exit 1
    fi
    ;;

  deploy)
    echo "Deploying iris_src/src/ to $CONTAINER..."
    docker exec "$CONTAINER" mkdir -p /tmp/src 2>/dev/null || true
    docker cp "$REPO_ROOT/iris_src/src/." "$CONTAINER:/tmp/src/"
    echo "✓ deployed iris_src/src/ → $CONTAINER:/tmp/src/"
    ;;

  compile)
    cls="${2:-}"
    if [ -z "$cls" ]; then echo "Usage: $0 compile <ClassName>"; exit 1; fi
    echo -n "Compiling $cls... "
    cls_path="/tmp/src/$(echo "$cls" | tr '.' '/').cls"
    docker exec "$CONTAINER" /usr/irissys/bin/irispython -c \
      "import iris; r=iris.cls('%SYSTEM.OBJ').Load('${cls_path}','ck'); print(r)" \
      2>&1 | grep -iE 'ERROR|Load finished|error #' | head -3
    ;;

  compile-all)
    echo "Compiling all Graph.KG.* classes..."
    # Use irispython (embedded Python) — it writes to the same database that
    # iris_devtester/external connections see. The old "iris session -U USER '...'"
    # form compiled into a different namespace mapping and changes were not visible
    # to external connections.
    docker exec "$CONTAINER" /usr/irissys/bin/irispython - << 'PYEOF' 2>&1 \
      | grep -iE 'ERROR|Compiling class|Detected|LoadDir|error #' | grep -v 'Warning\|PageRankEmbed\|Graph.KG.Edge'
import iris
result = iris.cls("%SYSTEM.OBJ").LoadDir("/tmp/src", "ck", None, 1)
print("LoadDir:", result)
PYEOF
    ;;

  tcp-deploy)
    # Deploy all ObjectScript classes via TCP connection so they are visible to
    # TCP test connections (port 31972). The HealthShare enterprise image routes
    # ^%Dictionary* globals differently for iris session vs TCP, so classes must
    # be compiled via TCP to be seen by external iris.connect() calls.
    python3 - << 'PYEOF'
import iris, os, glob, sys

port = int(os.environ.get("IVG_PORT", "31972"))
container = os.environ.get("IVG_ARNO_CONTAINER", "ivg-iris-enterprise")

try:
    conn = iris.connect(hostname="localhost", port=port, namespace="USER",
                        username="_SYSTEM", password="SYS")
except Exception as e:
    print(f"TCP connect failed (port {port}): {e}", file=sys.stderr)
    sys.exit(1)

irisobj = iris.createIRIS(conn)
cls_files = sorted(glob.glob("iris_src/src/**/*.cls", recursive=True))
print(f"Deploying {len(cls_files)} classes via TCP write+compile...")

errors = []
for cls_file in cls_files:
    with open(cls_file, "r") as f:
        content = f.read()
    rel = os.path.relpath(cls_file, "iris_src/src").replace(os.sep, "/")
    dest = f"/tmp/tcpsrc/{rel}"
    parent = os.path.dirname(dest)
    irisobj.classMethodValue("%File", "CreateDirectoryChain", parent)
    stream = irisobj.classMethodObject("%Stream.FileCharacter", "%New")
    stream.invokeVoid("LinkToFile", dest)
    for line in content.split("\n"):
        stream.invokeVoid("WriteLine", line)
    stream.invokeVoid("%Save")
    result = irisobj.classMethodValue("%SYSTEM.OBJ", "Load", dest, "ck-d")
    if not result:
        errors.append(cls_file)
        print(f"  ERROR: {cls_file}")

if errors:
    print(f"tcp-deploy: {len(errors)} compile error(s)")
    sys.exit(1)
else:
    print(f"✓ tcp-deploy: {len(cls_files)} classes deployed and compiled")
conn.close()
PYEOF
    ;;

  tcp-load-arno)
    # Load libarno_callout.so via TCP — the .so is a Docker volume mount not
    # visible to TCP IRIS sessions, so we write it via TCP's %Stream.FileBinary.
    python3 - << 'PYEOF'
import iris, os, json, sys

port = int(os.environ.get("IVG_PORT", "31972"))
so_path = "docker/enterprise/libarno_callout.so"

if not os.path.exists(so_path):
    print(f"tcp-load-arno: {so_path} not found", file=sys.stderr)
    sys.exit(1)

conn = iris.connect(hostname="localhost", port=port, namespace="USER",
                    username="_SYSTEM", password="SYS")
irisobj = iris.createIRIS(conn)

with open(so_path, "rb") as f:
    so_data = f.read()

stream = irisobj.classMethodObject("%Stream.FileBinary", "%New")
stream.invokeVoid("LinkToFile", "/tmp/libarno_tcp.so")
chunk_size = 32768
for i in range(0, len(so_data), chunk_size):
    stream.invokeVoid("Write", so_data[i:i+chunk_size])
stream.invokeVoid("%Save")

r1 = irisobj.classMethodValue("Graph.KG.ArnoAccel", "Load", "/tmp/libarno_tcp.so")
irisobj.classMethodValue("Graph.KG.NKGAccelLoader", "Load", "/tmp/libarno_tcp.so")
caps_str = irisobj.classMethodValue("Graph.KG.NKGAccel", "Capabilities")
caps = json.loads(str(caps_str))
if caps.get("rust_callout"):
    print("✓ tcp-load-arno: libarno_callout.so loaded (rust_callout=True)")
elif r1:
    print(f"⚠ tcp-load-arno: loaded but rust_callout=False: {caps}")
else:
    print("✗ tcp-load-arno: load FAILED", file=sys.stderr)
    sys.exit(1)
conn.close()
PYEOF
    ;;

  *)
    echo "Usage: $0 {up|down|status|deploy|compile <cls>|compile-all|tcp-deploy|tcp-load-arno}"
    exit 1
    ;;
esac
