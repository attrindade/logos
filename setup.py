#!/usr/bin/env python3
"""
Logos — Interactive Setup & TUI Installer (Minimalist & Clean)
Cross-platform support for Windows and Linux.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure root directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import requests
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import box

console = Console()

CONFIG_FILE = BASE_DIR / "logos_config.yaml"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_header():
    title = Text("✦ LOGOS ✦", style="bold cyan")
    subtitle = Text("Offline Voice-to-Vault Pipeline (Whisper → Ollama → Markdown)", style="dim white")
    console.print(
        Panel(
            Text.assemble(title, "\n", subtitle, justify="center"),
            box=box.ROUNDED,
            border_style="cyan",
            padding=(1, 2),
        )
    )


def check_ffmpeg() -> bool:
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        return res.returncode == 0
    except Exception:
        return False


def check_ollama(host: str = "http://localhost:11434") -> tuple[bool, list[str]]:
    try:
        r = requests.get(f"{host}/api/tags", timeout=2)
        if r.status_code == 200:
            models = [m.get("name") for m in r.json().get("models", [])]
            return True, models
    except Exception:
        pass
    return False, []


def step_check_dependencies():
    console.print("\n[bold cyan]1. Environment Diagnostics[/bold cyan]")
    
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold magenta")
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details / Action")

    # Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    table.add_row("Python", "[green]✔ OK[/green]", f"v{py_ver} ({sys.platform})")

    # ffmpeg
    ffmpeg_ok = check_ffmpeg()
    if ffmpeg_ok:
        table.add_row("ffmpeg", "[green]✔ OK[/green]", "Found in system PATH, ready for audio decoding")
    else:
        hint = "winget install Gyan.FFmpeg" if sys.platform == "win32" else "sudo apt install ffmpeg"
        table.add_row("ffmpeg", "[red]✖ Missing[/red]", f"[yellow]Required in PATH[/yellow] (e.g. {hint})")

    # Ollama
    ollama_ok, models = check_ollama()
    if ollama_ok:
        m_summary = f"{len(models)} local model(s)"
        table.add_row("Ollama Server", "[green]✔ Online[/green]", f"Available at localhost:11434 ({m_summary})")
    else:
        table.add_row(
            "Ollama Server",
            "[yellow]! Offline[/yellow]",
            "Not responding at localhost:11434 (Start Ollama or install from ollama.com)",
        )

    console.print(table)
    return ffmpeg_ok, ollama_ok, models


def step_setup_data_directory():
    console.print("\n[bold cyan]2. Storage & Working Directories[/bold cyan]")
    default_data_root = str(Path.home() / "Logos")
    
    console.print(
        f"[dim]The root folder stores Inbox (synced voice notes), Archive (audio backup),\n"
        f"Transcripts (verbatim text), Notes (categorized markdown), logs, and state.\n"
        f"Press Enter to keep the recommended default path, or type a custom directory.[/dim]\n"
    )
    
    data_root = Prompt.ask(
        "[bold white]Data root directory[/bold white] [dim yellow](Default)[/dim yellow]",
        default=default_data_root,
        show_default=True,
    )
    data_path = Path(data_root).expanduser().resolve()
    
    return str(data_path)


def step_setup_whisper(data_root: str):
    console.print("\n[bold cyan]3. Whisper Configuration (Local STT)[/bold cyan]")
    
    default_models_dir = str(Path(data_root) / "models")
    console.print(
        f"[dim]Folder where faster-whisper models will be cached or loaded.\n"
        f"Press Enter to use the default location inside your Logos folder.[/dim]\n"
    )
    
    models_dir = Prompt.ask(
        "[bold white]Whisper models directory[/bold white] [dim yellow](Default)[/dim yellow]",
        default=default_models_dir,
        show_default=True,
    )
    models_path = Path(models_dir).expanduser().resolve()
    models_path.mkdir(parents=True, exist_ok=True)

    console.print("\nModel options:")
    console.print("  [cyan]1[/cyan]) [bold]large-v3-turbo[/bold] [yellow](Default / Recommended)[/yellow] — ~1.6 GB, highest accuracy & speed")
    console.print("  [cyan]2[/cyan]) [bold]medium[/bold] — ~1.5 GB")
    console.print("  [cyan]3[/cyan]) [bold]small[/bold] — ~480 MB")
    console.print("  [cyan]4[/cyan]) [bold]base[/bold] — ~145 MB (ultra-lightweight)")
    
    choice = Prompt.ask(
        "\n[bold white]Select Whisper model size[/bold white] [dim yellow](Default: 1)[/dim yellow]",
        choices=["1", "2", "3", "4", "large-v3-turbo", "medium", "small", "base"],
        default="1",
        show_choices=False,
    )
    
    mapping = {
        "1": "large-v3-turbo",
        "2": "medium",
        "3": "small",
        "4": "base",
    }
    model_size = mapping.get(choice, choice)

    # Check if model files already exist
    existing_files = list(models_path.glob("**/*.bin")) + list(models_path.glob("**/*.safetensors"))
    has_local = len(existing_files) > 0

    if not has_local:
        if Confirm.ask(f"[bold yellow]Download '{model_size}' model now?[/bold yellow]", default=True):
            with console.status(f"[bold green]Downloading and initializing faster-whisper ({model_size})... Please wait.[/bold green]"):
                try:
                    from faster_whisper import WhisperModel
                    _ = WhisperModel(
                        model_size,
                        device="cpu",
                        compute_type="int8",
                        download_root=str(models_path),
                    )
                    console.print(f"[bold green]✔ Model '{model_size}' ready and cached in {models_path}![/bold green]")
                except Exception as e:
                    console.print(f"[bold red]✖ Automatic download failed: {e}[/bold red]")
                    console.print("[dim]No worries, Logos will attempt to download on first run.[/dim]")
    else:
        console.print(f"[green]✔ Model files already found in {models_path}![/green]")

    return str(models_path), model_size


def step_setup_ollama(ollama_models: list[str]):
    console.print("\n[bold cyan]4. LLM Configuration (Ollama)[/bold cyan]")
    
    default_model = "qwen2.5:3b-instruct-q4_K_M"
    
    if ollama_models:
        console.print(f"[dim]Models detected in your local Ollama: {', '.join(ollama_models)}[/dim]")
        if default_model in ollama_models:
            console.print(f"[green]✔ Recommended model '{default_model}' is already available![/green]")
    else:
        console.print(
            f"[dim]We recommend [bold cyan]{default_model}[/bold cyan] for optimal speed,\n"
            f"structured YAML adherence, and low RAM/disk footprint (~2.0 GB).[/dim]"
        )

    console.print(f"[dim]Press Enter to accept the default recommended model, or type another.[/dim]\n")
    llm_model = Prompt.ask(
        "[bold white]Ollama model name for note enrichment[/bold white] [dim yellow](Default)[/dim yellow]",
        default=default_model,
        show_default=True,
    )

    if ollama_models and llm_model not in ollama_models:
        console.print(f"[yellow]Notice: Model '{llm_model}' not found in local Ollama tags.[/yellow]")
        console.print(f"[dim]To download it, run in your terminal: [bold]ollama pull {llm_model}[/bold][/dim]")

    return llm_model


def step_setup_startup():
    console.print("\n[bold cyan]5. Background Service & Startup[/bold cyan]")
    hint = "via Windows Startup" if sys.platform == "win32" else "via systemd user service"
    enable_boot = Confirm.ask(
        f"[bold white]Configure Logos to start automatically in background ({hint})?[/bold white]",
        default=False,
    )
    
    if enable_boot:
        try:
            import install_startup
            install_startup.install()
        except Exception as e:
            console.print(f"[red]Failed to setup auto-startup: {e}[/red]")


def save_config(data_root: str, whisper_dir: str, whisper_size: str, llm_model: str):
    cfg = {
        "data_root": data_root,
        "whisper_models_dir": whisper_dir,
        "whisper_model_size": whisper_size,
        "whisper_compute_type": "int8",
        "whisper_cpu_threads": 6,
        "whisper_language": "pt",
        "whisper_vad_filter": True,
        "ollama_host": "http://localhost:11434",
        "ollama_num_ctx": 8192,
        "ollama_keep_alive": "0",
        "llm_model": llm_model,
        "chunk_token_threshold": 6000,
        "archive_retention_days": 60,
        "stability_check_interval_s": 5,
        "stability_check_count": 2,
        "process_priority_below_normal": True,
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    # Create directory tree
    d_path = Path(data_root)
    for sub in ["Inbox", "Archive", "Transcripts", "Notes/Diario", "Notes/Planejamento", "Notes/Inbox", "state", "logs", "models"]:
        (d_path / sub).mkdir(parents=True, exist_ok=True)

    console.print(f"\n[bold green]✔ Configuration saved to:[/bold green] {CONFIG_FILE}")


def main():
    clear_screen()
    print_header()
    
    ffmpeg_ok, ollama_ok, ollama_models = step_check_dependencies()
    
    data_root = step_setup_data_directory()
    whisper_dir, whisper_size = step_setup_whisper(data_root)
    llm_model = step_setup_ollama(ollama_models)
    
    save_config(data_root, whisper_dir, whisper_size, llm_model)
    
    step_setup_startup()

    summary_panel = Panel(
        Text.assemble(
            ("✦ Setup Completed Successfully! ✦\n\n", "bold green"),
            ("Inbox Directory (Syncthing Target): ", "bold white"), (f"{Path(data_root) / 'Inbox'}\n", "cyan"),
            ("Notes Output Directory: ", "bold white"), (f"{Path(data_root) / 'Notes'}\n\n", "cyan"),
            ("To start monitoring right now, run:\n", "white"),
            ("  python main.py\n\n", "bold yellow"),
            ("Refer to README.md for complete details and mobile sync setup.", "dim white"),
        ),
        box=box.ROUNDED,
        border_style="green",
        padding=(1, 2),
    )
    console.print("\n")
    console.print(summary_panel)


if __name__ == "__main__":
    main()

