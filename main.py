import os
import json
import time
import shutil
import subprocess
import asyncio
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from web3 import Web3
import aiofiles

# -------- Config --------
GETH_COMMAND = os.getenv("GETH_COMMAND", "geth")   # path to geth binary, default from package
BASE_DIR = os.path.abspath(".")
NODES_DIR = os.path.join(BASE_DIR, "geth_nodes")
OUT_DIR = os.path.join(BASE_DIR, "out")
NETWORK_ID = int(os.getenv("NETWORK_ID", "1515"))
CHA_ID = int(os.getenv("CHAIN_ID", "1515"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "18"))

# default miner/HTTP port base
HTTP_BASE = 8545
P2P_BASE = 30303

# create dirs
os.makedirs(NODES_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

app = FastAPI(title="Geth Multi-node Provisioner")

# ---------- Utilities ----------
def run(cmd, cwd=None, wait=True):
    """Run shell command, return CompletedProcess or Popen."""
    print("[cmd]", cmd)
    if wait:
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    else:
        return subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

async def read_file(path):
    async with aiofiles.open(path, "r") as f:
        return await f.read()

# ---------- Heartbeat ----------
async def heartbeat_task():
    while True:
        print(f"[heartbeat] alive @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
        await asyncio.sleep(HEARTBEAT_INTERVAL)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(heartbeat_task())

# ---------- Geth helpers ----------
def create_password_file(node_dir):
    pwfile = os.path.join(node_dir, "pw.txt")
    with open(pwfile, "w") as f:
        f.write("password\n")
    return pwfile

def create_account(node_dir):
    """Create a new account in this datadir and return the address (0x...)."""
    pw = create_password_file(node_dir)
    cmd = f"{GETH_COMMAND} --datadir {node_dir} account new --password {pw}"
    cp = run(cmd)
    out = cp.stdout.strip() + cp.stderr.strip()
    # parse address: "Address: {....}"
    addr = None
    for line in out.splitlines():
        if "Address" in line:
            # e.g. "Address: {0xabc...}"
            import re
            m = re.search(r'0x[0-9a-fA-F]+', line)
            if m:
                addr = m.group(0)
                break
    if not addr:
        raise RuntimeError(f"Could not create account: output={out}")
    return addr

def build_clique_genesis(chainId, signer_addresses, gasLimit=8000000):
    # extraData: 32 bytes vanity + list of signer addresses (20 bytes each) + 65 bytes zero
    vanity = "00" * 32
    signers_hex = "".join([addr.lower().replace("0x","") for addr in signer_addresses])
    tail = "00" * 65
    extra = "0x" + vanity + signers_hex + tail
    genesis = {
        "config": {
            "chainId": chainId,
            "clique": {"period": 1, "epoch": 30000},
            "ethash": {}
        },
        "nonce": "0x0",
        "timestamp": "0x0",
        "extraData": extra,
        "gasLimit": hex(gasLimit),
        "difficulty": "0x1",
        "mixhash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "coinbase": "0x0000000000000000000000000000000000000000",
        "alloc": {}
    }
    return genesis

def init_nodes(num_nodes, chain_id):
    """Create node dirs and accounts; return list of account addresses and node dir paths."""
    node_paths = []
    signer_addresses = []
    for i in range(1, num_nodes+1):
        node_dir = os.path.join(NODES_DIR, f"node{i}")
        datadir = os.path.join(node_dir, "data")
        os.makedirs(datadir, exist_ok=True)
        node_paths.append(node_dir)

        # create password file and account
        addr = create_account(datadir)
        signer_addresses.append(addr)
        print(f"[node {i}] created account {addr}")
    return node_paths, signer_addresses

def write_genesis(genesis_obj, dest_path):
    with open(dest_path, "w") as f:
        json.dump(genesis_obj, f, indent=2)
    print("[genesis] written to", dest_path)

def get_node_http_port(idx):
    return HTTP_BASE + (idx - 1)

def get_node_p2p_port(idx):
    return P2P_BASE + (idx - 1)

def init_geth_datadir(node_dir, genesis_path):
    datadir = os.path.join(node_dir, "data")
    cmd = f"{GETH_COMMAND} --datadir {datadir} init {genesis_path}"
    cp = run(cmd)
    if cp.returncode != 0:
        print("[init] stderr:", cp.stderr)
        raise RuntimeError(f"geth init failed for {node_dir}")

def start_geth_node(node_dir, idx, unlock_addr):
    datadir = os.path.join(node_dir, "data")
    http_port = get_node_http_port(idx)
    p2p_port = get_node_p2p_port(idx)
    pwfile = os.path.join(datadir, "pw.txt")
    logfile = os.path.join(node_dir, f"geth_{idx}.log")

    cmd = (
        f"{GETH_COMMAND} --datadir {datadir} "
        f"--networkid {NETWORK_ID} "
        f"--http --http.addr 127.0.0.1 --http.port {http_port} --http.api eth,net,web3,personal,admin "
        f"--port {p2p_port} "
        f"--allow-insecure-unlock --unlock {unlock_addr} --password {pwfile} --mine --miner.threads=1 "
        f"> {logfile} 2>&1 & echo $!"
    )
    # run and capture pid
    p = subprocess.Popen(cmd, shell=True, cwd=node_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    pid = out.strip()
    print(f"[start node {idx}] pid={pid} log={logfile}")
    return pid

def get_enode_via_rpc(http_port):
    """Query admin.nodeInfo.enode via RPC using web3.geth.admin.node_info()"""
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{http_port}"))
        # wait until connected
        attempts = 0
        while attempts < 10:
            try:
                info = w3.geth.admin.node_info()
                enode = info.get("enode")
                if enode:
                    return enode
            except Exception:
                attempts += 1
                time.sleep(1)
        raise RuntimeError("Could not get enode from node at http port " + str(http_port))
    except Exception as e:
        raise

def add_peer_via_rpc(http_port, enode):
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{http_port}"))
        # call admin.addPeer
        return w3.geth.admin.add_peer(enode)
    except Exception as e:
        print("[add_peer_via_rpc error]", e)
        return False

# ---------- High-level provision flow ----------
def provision_cluster(num_nodes: int = 3, chain_id: int = CHA_ID):
    # Step 1: create node dirs & accounts
    node_paths, signer_addrs = init_nodes(num_nodes, chain_id)
    print("[provision] signer addresses:", signer_addrs)

    # Step 2: generate genesis.json with signer addresses
    genesis = build_clique_genesis(chain_id, signer_addrs)
    genesis_path = os.path.join(OUT_DIR, f"genesis_{int(time.time())}.json")
    write_genesis(genesis, genesis_path)

    # Step 3: init each datadir with genesis
    for node_dir in node_paths:
        init_geth_datadir(node_dir, genesis_path)

    # Step 4: start nodes (first node as boot/seed)
    pids = []
    for idx, node_dir in enumerate(node_paths, start=1):
        unlock_addr = signer_addrs[idx - 1]
        pid = start_geth_node(node_dir, idx, unlock_addr)
        pids.append(pid)
        # small sleep to permit node to start and expose HTTP admin
        time.sleep(2)

    # Step 5: fetch enode of first node and connect peers
    boot_http_port = get_node_http_port(1)
    enode = get_enode_via_rpc(boot_http_port)
    print("[provision] boot enode:", enode)

    # Add bootnode to other nodes via RPC
    for idx in range(2, num_nodes + 1):
        other_port = get_node_http_port(idx)
        ok = add_peer_via_rpc(other_port, enode)
        print(f"[provision] add_peer node{idx} <- boot : {ok}")

    # Step 6: save cluster metadata
    cluster_meta = {
        "timestamp": int(time.time()),
        "num_nodes": num_nodes,
        "signers": signer_addrs,
        "genesis": genesis_path,
        "pids": pids
    }
    meta_path = os.path.join(OUT_DIR, f"cluster_{int(time.time())}.json")
    with open(meta_path, "w") as f:
        json.dump(cluster_meta, f, indent=2)

    return {"meta": cluster_meta, "meta_path": meta_path}

# ---------- FastAPI endpoints ----------
@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        async with aiofiles.open("static_index.html", "r") as f:
            return await f.read()
    except Exception:
        return "<h1>Geth Provisioner</h1><p>Use /provision to create the cluster.</p>"

@app.post("/provision")
def api_provision(num_nodes: int = Form(3), chain_id: int = Form(CHA_ID)):
    try:
        res = provision_cluster(num_nodes, chain_id)
        return JSONResponse({"status": "ok", **res})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})

@app.get("/nodes")
def api_nodes():
    nodes = []
    if os.path.exists(NODES_DIR):
        for name in sorted(os.listdir(NODES_DIR)):
            path = os.path.join(NODES_DIR, name)
            nodes.append({"name": name, "path": path})
    return {"nodes": nodes}

@app.get("/health")
def api_health():
    return {"status": "ok", "time": time.time()}

# End of main.py
