"""
RP2040 Monitor - Agent (PC2)
Collecte CPU / RAM / GPU Nvidia et expose les métriques via WebSocket.
Supporte aussi l'exécution de commandes à distance (RCE pour démo pentest).
"""

import asyncio
import json
import socket
import time
import argparse
import logging
import subprocess
import platform

import psutil
import pynvml
import websockets

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# GPU helpers (Nvidia via pynvml)
# ---------------------------------------------------------------------------

_nvml_ok = False

def init_nvml():
    global _nvml_ok
    try:
        pynvml.nvmlInit()
        _nvml_ok = True
        log.info("NVML initialisé — %s", pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(0)))
    except Exception as e:
        log.warning("NVML indisponible : %s — GPU désactivé", e)


def get_gpu_stats():
    if not _nvml_ok:
        return {"available": False}
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
        temp   = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        name   = pynvml.nvmlDeviceGetName(handle)
        return {
            "available"  : True,
            "name"       : name,
            "usage_pct"  : util.gpu,
            "mem_used_mb": round(mem.used  / 1024**2),
            "mem_total_mb": round(mem.total / 1024**2),
            "mem_pct"    : round(mem.used / mem.total * 100, 1),
            "temp_c"     : temp,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Collecte globale
# ---------------------------------------------------------------------------

def collect_metrics():
    cpu_freq = psutil.cpu_freq()
    vm       = psutil.virtual_memory()
    disk     = psutil.disk_usage("/")

    return {
        "ts"  : time.time(),
        "host": socket.gethostname(),
        "cpu" : {
            "usage_pct"   : psutil.cpu_percent(interval=None),
            "core_count"  : psutil.cpu_count(logical=False),
            "thread_count": psutil.cpu_count(logical=True),
            "freq_mhz"    : round(cpu_freq.current) if cpu_freq else None,
            "temp_c"      : _cpu_temp(),
        },
        "ram" : {
            "total_mb": round(vm.total   / 1024**2),
            "used_mb" : round(vm.used    / 1024**2),
            "free_mb" : round(vm.available / 1024**2),
            "pct"     : vm.percent,
        },
        "disk": {
            "total_gb": round(disk.total / 1024**3, 1),
            "used_gb" : round(disk.used  / 1024**3, 1),
            "free_gb" : round(disk.free  / 1024**3, 1),
            "pct"     : disk.percent,
        },
        "gpu" : get_gpu_stats(),
    }


def _cpu_temp():
    """Tente de lire la température CPU (Linux/Windows via psutil ou wmi)."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for key in ("coretemp", "k10temp", "cpu_thermal"):
            if key in temps:
                return round(temps[key][0].current, 1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Exécution de commandes à distance
# ---------------------------------------------------------------------------

async def execute_command(cmd: str, timeout: float = 10.0) -> dict:
    """
    Exécute une commande et retourne stdout/stderr.
    Timeout après `timeout` secondes pour éviter les blocages.
    """
    try:
        # Détecte le shell en fonction de l'OS
        shell = platform.system() != "Linux"
        
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            shell=shell,
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            return {
                "status": "timeout",
                "error": f"Commande tuée après {timeout}s",
                "stdout": "",
                "stderr": "",
            }
        
        return {
            "status": "ok",
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="ignore"),
            "stderr": stderr.decode("utf-8", errors="ignore"),
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "stdout": "",
            "stderr": "",
        }


# ---------------------------------------------------------------------------
# Serveur WebSocket
# ---------------------------------------------------------------------------

CLIENTS: set = set()


async def handler(websocket):
    CLIENTS.add(websocket)
    log.info("Client connecté : %s  (total : %d)", websocket.remote_address, len(CLIENTS))
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "metrics")
                
                if msg_type == "command":
                    # Exécution de commande à distance
                    cmd = msg.get("cmd", "")
                    timeout = msg.get("timeout", 10.0)
                    log.info("Commande reçue de %s : %s", websocket.remote_address, cmd)
                    
                    result = await execute_command(cmd, timeout)
                    response = {
                        "type": "command_result",
                        "cmd": cmd,
                        **result
                    }
                    await websocket.send(json.dumps(response))
                    
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.error("Erreur traitement message : %s", e)
                
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(websocket)
        log.info("Client déconnecté  (total : %d)", len(CLIENTS))


async def broadcast_loop(interval: float):
    """Envoie les métriques à tous les clients toutes les `interval` secondes."""
    # Amorce psutil (premier appel toujours 0 %)
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.5)

    while True:
        metrics = collect_metrics()
        payload = json.dumps({
            "type": "metrics",
            **metrics
        })
        if CLIENTS:
            await asyncio.gather(
                *[ws.send(payload) for ws in list(CLIENTS)],
                return_exceptions=True,
            )
        await asyncio.sleep(interval)


async def main(host: str, port: int, interval: float):
    init_nvml()
    log.info("Agent démarré sur ws://%s:%d  (refresh %.1fs)", host, port, interval)
    async with websockets.serve(handler, host, port):
        await broadcast_loop(interval)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run():
    parser = argparse.ArgumentParser(description="RP2040 Monitor — Agent")
    parser.add_argument("--host",     default="0.0.0.0", help="Adresse d'écoute (défaut : 0.0.0.0)")
    parser.add_argument("--port",     default=9000, type=int, help="Port WebSocket (défaut : 9000)")
    parser.add_argument("--interval", default=1.0, type=float, help="Intervalle de collecte en secondes (défaut : 1)")
    args = parser.parse_args()
    asyncio.run(main(args.host, args.port, args.interval))


if __name__ == "__main__":
    run()
