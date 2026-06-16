"""
RP2040 Monitor - Dashboard TUI (PC1)
Se connecte à l'agent via WebSocket et affiche les métriques en temps réel.
Inclut aussi un terminal à distance pour exécuter des commandes sur PC2.
"""

import asyncio
import json
import os
import sys
import argparse
import time
from collections import deque
from datetime import datetime

import websockets
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text
from rich import box
from rich.input import Prompt

console = Console()

# Historique pour mini-graphe ASCII
HISTORY_LEN = 40
cpu_hist = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
ram_hist = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
gpu_hist = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)

_last_data: dict = {}
_status: str = "Connexion en cours…"
_latency_ms: float = 0.0
_command_output: str = ""
_command_error: str = ""
_websocket = None


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------

def sparkline(values: deque, width: int = 38) -> str:
    """Mini graphe ASCII avec les blocs ▁▂▃▄▅▆▇█."""
    bars = " ▁▂▃▄▅▆▇█"
    vals = list(values)[-width:]
    if not vals or max(vals) == 0:
        return " " * len(vals)
    hi = max(vals)
    return "".join(bars[min(int(v / hi * 8), 8)] for v in vals)


def color_pct(pct: float) -> str:
    if pct >= 85:
        return "bold red"
    if pct >= 60:
        return "bold yellow"
    return "bold green"


def make_bar(pct: float, width: int = 30) -> Text:
    filled  = int(pct / 100 * width)
    bar_str = "█" * filled + "░" * (width - filled)
    return Text(bar_str, style=color_pct(pct))


def build_layout(data: dict) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="terminal", size=12),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── Header ──────────────────────────────────────────────────────────────
    ts   = datetime.fromtimestamp(data.get("ts", time.time())).strftime("%H:%M:%S")
    host = data.get("host", "—")
    header_txt = Text.assemble(
        ("  RP2040 MONITOR  ", "bold white on blue"),
        f"   🖥  {host}   ",
        ("🕐 " + ts, "cyan"),
        f"   latence : {_latency_ms:.0f} ms",
    )
    layout["header"].update(Panel(header_txt, box=box.HORIZONTALS))

    # ── CPU ─────────────────────────────────────────────────────────────────
    cpu  = data.get("cpu", {})
    cpup = cpu.get("usage_pct", 0)
    grid_cpu = Table.grid(padding=(0, 1))
    grid_cpu.add_column(width=10)
    grid_cpu.add_column()
    grid_cpu.add_row("Usage",    Text.assemble(make_bar(cpup), f"  {cpup:5.1f}%", style=color_pct(cpup)))
    grid_cpu.add_row("Fréquence", f"{cpu.get('freq_mhz', '—')} MHz")
    grid_cpu.add_row("Cœurs",    f"{cpu.get('core_count','—')}C / {cpu.get('thread_count','—')}T")
    temp_c = cpu.get("temp_c")
    grid_cpu.add_row("Temp",     f"{temp_c} °C" if temp_c else "N/A")
    grid_cpu.add_row("Historique", Text(sparkline(cpu_hist), style="cyan"))

    # ── RAM ─────────────────────────────────────────────────────────────────
    ram  = data.get("ram", {})
    ramp = ram.get("pct", 0)
    grid_ram = Table.grid(padding=(0, 1))
    grid_ram.add_column(width=10)
    grid_ram.add_column()
    grid_ram.add_row("Usage",     Text.assemble(make_bar(ramp), f"  {ramp:5.1f}%", style=color_pct(ramp)))
    grid_ram.add_row("Utilisée",  f"{ram.get('used_mb','—')} Mo")
    grid_ram.add_row("Libre",     f"{ram.get('free_mb','—')} Mo")
    grid_ram.add_row("Total",     f"{ram.get('total_mb','—')} Mo")
    grid_ram.add_row("Historique", Text(sparkline(ram_hist), style="magenta"))

    left_tbl = Table.grid()
    left_tbl.add_row(Panel(grid_cpu, title="[bold cyan]CPU[/]",  box=box.ROUNDED))
    left_tbl.add_row(Panel(grid_ram, title="[bold magenta]RAM[/]", box=box.ROUNDED))
    layout["left"].update(left_tbl)

    # ── GPU ─────────────────────────────────────────────────────────────────
    gpu = data.get("gpu", {})
    if gpu.get("available"):
        gup   = gpu.get("usage_pct",  0)
        gmemp = gpu.get("mem_pct",     0)
        grid_gpu = Table.grid(padding=(0, 1))
        grid_gpu.add_column(width=10)
        grid_gpu.add_column()
        grid_gpu.add_row("Modèle",    gpu.get("name", "—"))
        grid_gpu.add_row("GPU",       Text.assemble(make_bar(gup), f"  {gup:5.1f}%", style=color_pct(gup)))
        grid_gpu.add_row("VRAM",      Text.assemble(make_bar(gmemp),
                                        f"  {gpu.get('mem_used_mb','—')} / {gpu.get('mem_total_mb','—')} Mo",
                                        style=color_pct(gmemp)))
        grid_gpu.add_row("Temp",      f"{gpu.get('temp_c','—')} °C")
        grid_gpu.add_row("Historique", Text(sparkline(gpu_hist), style="yellow"))
        gpu_panel = Panel(grid_gpu, title="[bold yellow]GPU Nvidia[/]", box=box.ROUNDED)
    else:
        gpu_panel = Panel("[dim]GPU non disponible[/]", title="[bold yellow]GPU[/]", box=box.ROUNDED)

    # ── Disque ──────────────────────────────────────────────────────────────
    disk = data.get("disk", {})
    dp   = disk.get("pct", 0)
    grid_disk = Table.grid(padding=(0, 1))
    grid_disk.add_column(width=10)
    grid_disk.add_column()
    grid_disk.add_row("Usage",    Text.assemble(make_bar(dp), f"  {dp:5.1f}%", style=color_pct(dp)))
    grid_disk.add_row("Utilisé",  f"{disk.get('used_gb','—')} Go")
    grid_disk.add_row("Libre",    f"{disk.get('free_gb','—')} Go")
    grid_disk.add_row("Total",    f"{disk.get('total_gb','—')} Go")
    disk_panel = Panel(grid_disk, title="[bold green]Disque[/]", box=box.ROUNDED)

    right_col = Table.grid()
    right_col.add_row(gpu_panel)
    right_col.add_row(disk_panel)
    layout["right"].update(right_col)

    # ── Terminal à distance ──────────────────────────────────────────────────
    term_content = ""
    if _command_error:
        term_content += f"[bold red]❌ Erreur :[/] {_command_error}\n\n"
    if _command_output:
        term_content += f"[cyan]$ Résultat :[/]\n{_command_output[:500]}"
    else:
        term_content += "[dim]En attente de commande...[/]"
    
    layout["terminal"].update(Panel(
        term_content,
        title="[bold yellow]🖥  Terminal à distance (taper 'cmd' pour exécuter)[/]",
        box=box.ROUNDED,
    ))

    # ── Footer ──────────────────────────────────────────────────────────────
    layout["footer"].update(Panel(
        Text.assemble(
            ("  q", "bold yellow"), " quitter   ",
            ("  Ctrl+C", "bold yellow"), " reconnecter   ",
            "   status : ", (_status, "cyan")
        ),
        box=box.HORIZONTALS,
    ))

    return layout


# ---------------------------------------------------------------------------
# Boucle WebSocket
# ---------------------------------------------------------------------------

async def send_command(cmd: str):
    """Envoie une commande à l'agent et attend le résultat."""
    global _command_output, _command_error, _websocket
    
    if not _websocket:
        _command_error = "Pas connecté à l'agent"
        return
    
    try:
        msg = {
            "type": "command",
            "cmd": cmd,
            "timeout": 10.0
        }
        await _websocket.send(json.dumps(msg))
    except Exception as e:
        _command_error = str(e)


async def receive_loop(uri: str, live: Live):
    global _status, _latency_ms, _last_data, _command_output, _command_error, _websocket

    reconnect_delay = 2

    while True:
        try:
            _status = f"Connexion à {uri}…"
            async with websockets.connect(uri, open_timeout=5) as ws:
                _websocket = ws
                _status = f"Connecté ✓  ({uri})"
                reconnect_delay = 2
                async for raw in ws:
                    t0 = time.perf_counter()
                    data = json.loads(raw)
                    _latency_ms = (time.perf_counter() - t0) * 1000

                    msg_type = data.get("type", "metrics")
                    
                    if msg_type == "metrics":
                        # Mise à jour des métriques
                        cpu_hist.append(data.get("cpu", {}).get("usage_pct", 0))
                        ram_hist.append(data.get("ram", {}).get("pct", 0))
                        if data.get("gpu", {}).get("available"):
                            gpu_hist.append(data["gpu"]["usage_pct"])
                        _last_data = data
                        live.update(build_layout(data))
                    
                    elif msg_type == "command_result":
                        # Résultat de commande
                        cmd = data.get("cmd", "")
                        status = data.get("status", "unknown")
                        if status == "ok":
                            _command_output = data.get("stdout", "") or data.get("stderr", "")
                            _command_error = ""
                        else:
                            _command_error = data.get("error", "Erreur inconnue")
                            _command_output = ""
                        live.update(build_layout(_last_data))

        except (OSError, websockets.exceptions.WebSocketException) as e:
            _status = f"Erreur : {e}  — reconnexion dans {reconnect_delay}s"
            _websocket = None
            if _last_data:
                live.update(build_layout(_last_data))
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)


async def command_input_loop(live: Live):
    """Boucle pour saisie de commandes dans le terminal."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            # Saisie bloquante dans un thread pour ne pas bloquer asyncio
            cmd = await loop.run_in_executor(None, lambda: input("$ "))
            if cmd.lower() == "q":
                return
            if cmd.strip():
                await send_command(cmd)
                await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            return
        except EOFError:
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run():
    parser = argparse.ArgumentParser(description="RP2040 Monitor — Dashboard TUI")
    parser.add_argument("host", help="IP ou hostname du PC cible (PC2)")
    parser.add_argument("--port", default=9000, type=int, help="Port WebSocket (défaut : 9000)")
    args = parser.parse_args()

    uri = f"ws://{args.host}:{args.port}"

    empty = {"ts": time.time(), "host": args.host, "cpu": {}, "ram": {}, "disk": {}, "gpu": {}}

    async def main_loop():
        with Live(build_layout(empty), console=console, refresh_per_second=2, screen=True) as live:
            try:
                await asyncio.gather(
                    receive_loop(uri, live),
                    command_input_loop(live),
                    return_exceptions=True,
                )
            except KeyboardInterrupt:
                pass

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass

    console.print("[bold green]Dashboard fermé.[/]")


if __name__ == "__main__":
    run()
