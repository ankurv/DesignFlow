import sqlite3
import time
import requests
import json

API_URL = "http://localhost:8000"

def run_simulation():
    session = requests.Session()
    print("Logging in...")
    r = session.post(f"{API_URL}/auth/login", json={"username": "admin", "password": "admin"})
    r.raise_for_status()

    print("Opening project chatbay...")
    r = session.post(f"{API_URL}/project/open", json={"path": "/Users/avarsheny/chatbay"})
    r.raise_for_status()

    print("Copying agents from AlphaDrive to chatbay and unpausing them...")
    db_src = sqlite3.connect("/Users/avarsheny/AlphaDrive/.designflow/designflow.db")
    db_dst = sqlite3.connect("/Users/avarsheny/chatbay/.designflow/designflow.db")
    agents = db_src.execute("SELECT id, sort_order, config_json FROM agents").fetchall()
    
    # Clear default agents in chatbay
    db_dst.execute("DELETE FROM agents")
    for agent in agents:
        agent_id, sort_order, config_str = agent
        config = json.loads(config_str)
        config["is_paused"] = False
        db_dst.execute(
            "INSERT INTO agents (id, sort_order, config_json) VALUES (?, ?, ?)",
            (agent_id, sort_order, json.dumps(config))
        )
    db_dst.commit()
    db_src.close()
    db_dst.close()

    print("Generating MCP Token...")
    r = session.post(f"{API_URL}/mcp/access-token")
    r.raise_for_status()
    token = r.json()["token"]
    print(f"MCP Token generated: {token}")

    print("Resetting previous run...")
    session.post(f"{API_URL}/run/reset")

    print("Starting design workflow...")
    r = session.post(f"{API_URL}/run/start", json={"idea": "design a whatsapp like product", "mode": "auto"})
    if r.status_code != 200:
        print(f"Error starting workflow: {r.status_code} - {r.text}")
    r.raise_for_status()

    print("Observing via MCP Server...")
    mcp_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    while True:
        r = session.post(f"{API_URL}/mcp/", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_project_status",
                "arguments": {"project_path": "/Users/avarsheny/chatbay"}
            }
        }, headers=mcp_headers)
        
        if r.status_code != 200:
            print(f"MCP Request failed: {r.status_code} - {r.text}")
            break
            
        data = r.json()
        if "result" in data and "content" in data["result"]:
            status_content = json.loads(data["result"]["content"][0]["text"])
            run_info = status_content.get("latest_run", {})
            status = run_info.get("status")
            print(f"Run status: {status}")
            
            if status == "needs_attention":
                print("Run needs attention! Answering current checkpoint to approve...")
                cp_r = session.get(f"{API_URL}/run/checkpoint/current")
                if cp_r.status_code == 200 and cp_r.json():
                    cp_id = cp_r.json().get("id")
                    session.post(f"{API_URL}/run/checkpoint/{cp_id}/answer", json={"answer": "Approve all designs and proceed."})
                    print("Approved checkpoint.")
            
            if status in ("completed", "failed", "stopped"):
                print("Workflow finished.")
                break
        else:
            print("MCP Response missing content:", data)
        
        time.sleep(3)

if __name__ == "__main__":
    run_simulation()
