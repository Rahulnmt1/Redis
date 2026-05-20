"""
Generate professional architecture / data-flow diagrams for the HDFC Sec RDI demo.

Uses the `diagrams` Python library (https://diagrams.mingrammer.com/) which renders
Graphviz under the hood with branded service icons.

Outputs PNG + SVG into `docs/diagrams/`.

Run with:
    /tmp/rdi-diagrams-venv/bin/python3 docs/diagrams/generate_diagrams.py
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.onprem.client import Users
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis
from diagrams.programming.framework import Flask
from diagrams.programming.language import Python

# Redis brand-aligned palette
REDIS_RED = "#DC382C"
PG_BLUE = "#336791"
AMBER = "#F59E0B"
GREEN = "#2C8C2C"
GREY = "#6B7280"

OUT_DIR = Path(__file__).parent
OUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_GRAPH_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "16",
    "bgcolor": "white",
    "pad": "0.6",
    "nodesep": "0.65",
    "ranksep": "1.1",
    "splines": "spline",
    "labelloc": "t",
    "concentrate": "false",
}

BASE_NODE_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "12",
}

BASE_EDGE_ATTR = {
    "fontname": "Helvetica",
    "fontsize": "11",
    "color": GREY,
}


# ---------------------------------------------------------------------------
# Diagram 1 — High-level architecture (LR, clean, numbered headline flow)
# ---------------------------------------------------------------------------
with Diagram(
    "HDFC Securities · RDI Demo · Architecture & Request Flow",
    filename=str(OUT_DIR / "architecture"),
    outformat=["png", "svg"],
    show=False,
    direction="LR",
    graph_attr={
        **BASE_GRAPH_ATTR,
        "fontsize": "22",
        "ranksep": "1.4",
        "nodesep": "0.7",
    },
    node_attr=BASE_NODE_ATTR,
    edge_attr=BASE_EDGE_ATTR,
):

    # ----- Layer 1: System of Record -----
    with Cluster(
        "① System of Record  ·  stand-in for HDFC's Oracle",
        graph_attr={"bgcolor": "#EFF6FF", "style": "rounded", "color": PG_BLUE,
                    "fontcolor": PG_BLUE, "fontsize": "15", "margin": "20"},
    ):
        pg = PostgreSQL("PostgreSQL 16\nwal_level=logical · pgoutput\n:5432\n\nportfolio.customer\nportfolio.holding\nportfolio.trade\nportfolio.security_master\nportfolio.market_price")
        ops = Users("Back-office /\ntrading engine")
        ops >> Edge(label="SQL writes", color=PG_BLUE) >> pg

    # ----- Layer 2: RDI pipeline -----
    with Cluster(
        "② Redis Data Integration  ·  CDC pipeline",
        graph_attr={"bgcolor": "#FEF3C7", "style": "rounded", "color": AMBER,
                    "fontcolor": "#92400E", "fontsize": "15", "margin": "20"},
    ):
        dbz = Server("Debezium Server\n(same engine bundled\ninside production RDI)")
        state = Redis("Redis Enterprise\nrdi-state BDB · :12001\nstreams · offsets\nschema history")
        proc = Python("RDI Stream Processor\nrdi/jobs/*.yaml\nJMESPath transforms")
        api = Server("RDI Control-plane\nREST API\n/api/v1/login\n/api/v1/pipelines\n/api/v1/monitoring/*")

        dbz >> Edge(label="CDC events\n(at-least-once)", color=AMBER, penwidth="2") >> state
        state >> Edge(label="XREADGROUP", color=AMBER, penwidth="2") >> proc
        state - Edge(style="dashed", label="reads pipeline\nstate", color=GREY) - api

    # ----- Layer 3: Target Cache -----
    with Cluster(
        "③ Target Cache  ·  sub-millisecond reads for the trading app",
        graph_attr={"bgcolor": "#FEE2E2", "style": "rounded", "color": REDIS_RED,
                    "fontcolor": REDIS_RED, "fontsize": "15", "margin": "20"},
    ):
        target = Redis("Redis Enterprise\nportfolio-cache BDB\n:12000\n\nRedisJSON\nRediSearch\nStreams")
        idx = Server("FT indexes\ncust-idx · TAG · TEXT · NUMERIC\nhold-idx · NUMERIC\n\nHot keys\ncustomer:<cc>       JSON\nholding:<cid>:<sid> JSON\nprice:<sid>         HASH\nsecurity:<sym>      HASH\ntrades:<cid>        STREAM")
        target - Edge(style="dotted", color=REDIS_RED) - idx

    # ----- Layer 4: UIs (drawn AFTER target so they're physically to its right) -----
    with Cluster(
        "④ Demo UIs",
        graph_attr={"bgcolor": "#ECFDF5", "style": "rounded", "color": GREEN,
                    "fontcolor": GREEN, "fontsize": "15", "margin": "20"},
    ):
        dash = Flask("Portfolio Dashboard\nFlask · :5050")
        insight = Server("Redis Insight\n:5540\n(same tool\nused in prod)")
        trader = Users("Trader\n(Chrome)")
        trader >> Edge(label="HTTPS", color=GREY) >> dash

    # ----- Headline data flow (numbered, only the main path) -----
    pg >> Edge(label="A · logical replication\n(WAL via pgoutput)",
               color=PG_BLUE, penwidth="3") >> dbz
    proc >> Edge(label="B · JSON.SET · HSET · XADD\nper rdi/jobs/*.yaml",
                 color=REDIS_RED, penwidth="3") >> target

    # Read path
    dash >> Edge(label="C · FT.SEARCH · JSON.GET\npipelined HGETALL",
                 color=REDIS_RED, penwidth="3") >> target
    dash >> Edge(label="D · SQL\n(side-by-side latency cmp)",
                 color=PG_BLUE, style="dashed") >> pg

    # Ops path (single arrow into the RDI cluster, single into target)
    insight >> Edge(label="E · monitor target\nkeys + slowlog", color=GREEN, penwidth="2") >> target
    insight >> Edge(label="F · RDI tab\n(pipeline status)", color=GREEN, penwidth="2") >> api


# ---------------------------------------------------------------------------
# Diagram 2 — CDC write path (a row changes in Postgres)
# ---------------------------------------------------------------------------
with Diagram(
    "HDFC Securities · RDI Demo · CDC Write Path  (Postgres → Redis)",
    filename=str(OUT_DIR / "cdc-write-path"),
    outformat=["png", "svg"],
    show=False,
    direction="LR",
    graph_attr={**BASE_GRAPH_ATTR, "fontsize": "22"},
    node_attr=BASE_NODE_ATTR,
    edge_attr=BASE_EDGE_ATTR,
):

    ops = Users("Back-office /\ntrading engine")
    pg = PostgreSQL("PostgreSQL\nportfolio.trade\nportfolio.holding\nportfolio.customer")
    dbz = Server("Debezium Server\npgoutput")
    state = Redis("rdi-state BDB :12001\nstream:\ndata.portfolio.<table>")
    proc = Python("RDI Stream Processor\napplies\nrdi/jobs/<table>.yaml")
    target = Redis("portfolio-cache BDB\n:12000\nJSON + Search + Streams")
    dash = Flask("Portfolio\nDashboard")

    ops >> Edge(label="① INSERT / UPDATE\nportfolio.trade",
                color=PG_BLUE, penwidth="2.5", fontsize="12") >> pg
    pg >> Edge(label="② WAL emits\nlogical change record",
               color=PG_BLUE, penwidth="2.5", fontsize="12") >> dbz
    dbz >> Edge(label="③ XADD\ndata.portfolio.trade",
                color=AMBER, penwidth="2.5", fontsize="12") >> state
    state >> Edge(label="④ XREADGROUP\nconsumer group",
                  color=AMBER, penwidth="2.5", fontsize="12") >> proc
    proc >> Edge(label="⑤ apply trade.yaml\nJMESPath transform",
                 color=AMBER, style="dashed", fontsize="12") >> proc
    proc >> Edge(label="⑥ JSON.SET trade:<id>\nXADD trades:<cid>",
                 color=REDIS_RED, penwidth="2.5", fontsize="12") >> target
    target >> Edge(label="⑦ trader sees the trade\nin < 10 ms",
                   color=REDIS_RED, penwidth="2.5", fontsize="12") >> dash


# ---------------------------------------------------------------------------
# Diagram 3 — Dashboard read path (trader loads portfolio)
# ---------------------------------------------------------------------------
with Diagram(
    "HDFC Securities · RDI Demo · Dashboard Read Path  (Trader → Redis)",
    filename=str(OUT_DIR / "dashboard-read-path"),
    outformat=["png", "svg"],
    show=False,
    direction="LR",
    graph_attr={**BASE_GRAPH_ATTR, "fontsize": "22"},
    node_attr=BASE_NODE_ATTR,
    edge_attr=BASE_EDGE_ATTR,
):

    trader = Users("Trader\non browser")
    dash = Flask("Portfolio Dashboard\nFlask · :5050")

    with Cluster(
        "Inside the target Redis Enterprise BDB  ·  portfolio-cache :12000",
        graph_attr={"bgcolor": "#FEE2E2", "style": "rounded", "color": REDIS_RED,
                    "fontcolor": REDIS_RED, "fontsize": "14", "margin": "16"},
    ):
        cust_idx = Server("FT cust-idx\n@pan TAG · @client_code TAG\n@name TEXT")
        hold_idx = Server("FT hold-idx\n@customer_id NUMERIC")
        keys = Server("Hot keys\ncustomer:<cc>       JSON\nholding:<cid>:<sid> JSON\nprice:<sid>         HASH\nsecurity:<sym>      HASH")

    pg = PostgreSQL("PostgreSQL\n(side-by-side\nlatency comparison)")

    trader >> Edge(label="① GET /  +  search 'Raj'",
                   color=GREY, penwidth="2.5", fontsize="12") >> dash
    dash >> Edge(label="② FT.SEARCH cust-idx\n@name:Raj*  LIMIT 0 25",
                 color=REDIS_RED, penwidth="2.5", fontsize="12") >> cust_idx
    dash >> Edge(label="③ FT.SEARCH hold-idx\n@customer_id:[X X]",
                 color=REDIS_RED, penwidth="2.5", fontsize="12") >> hold_idx
    dash >> Edge(label="④ pipelined JSON.GET\nholding:<cid>:<sid>",
                 color=REDIS_RED, penwidth="2.5", fontsize="12") >> keys
    dash >> Edge(label="⑤ pipelined HGETALL\nprice:<sid> + security:<sym>",
                 color=REDIS_RED, penwidth="2.5", fontsize="12") >> keys
    dash >> Edge(label="⑥ render HTML\n(p95 < 25 ms)",
                 color=GREY, penwidth="2.5", fontsize="12") >> trader
    dash >> Edge(label="optional · same query\nin SQL for the side panel",
                 color=PG_BLUE, style="dashed", fontsize="11") >> pg


print(f"✓ Diagrams written to {OUT_DIR}")
for f in sorted(OUT_DIR.glob("*")):
    if f.suffix in {".png", ".svg"}:
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
