from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.console import Console
from rich.text import Text
from rich.align import Align
from rich.live import Live
from datetime import datetime

ASCII_ART = r"""
[cyan]  ______     __   _       __      __       __  
 /_  __/__  / /__| |     / /___ _/ /______/ /_ 
  / / / _ \/ / _ \ | /| / / __ `/ __/ ___/ __ \
 / / /  __/ /  __/ |/ |/ / /_/ / /_/ /__/ / / /
/_/  \___/_/\___/|__/|__/\__,_/\__/\___/_/ /_/ [/cyan]
"""

class Dashboard:
    def __init__(self, title="TeleWatch Monitor"):
        self.console = Console()
        self.title = title
        self.layout = Layout()
        self.setup_layout()
        
    def setup_layout(self):
        """Define the dashboard layout."""
        self.layout.split(
            Layout(name="header", size=10),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        self.layout["main"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=1)
        )
        
    def generate_header(self, process_name: str, status: str, profiler_progress: float = 0.0):
        """Generate the header panel with ASCII art."""
        status_color = "green" if status == "running" else "red"
        
        status_text = status.upper()
        if 0 < profiler_progress < 1.0:
            status_text = f"PROFILING {int(profiler_progress * 100)}%"
            status_color = "yellow"

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)
        
        # Right side: Status and Process
        info_table = Table.grid(padding=(0, 1))
        info_table.add_column(justify="right")
        info_table.add_row(f"[b white]{process_name}[/b white]")
        info_table.add_row(
            f"[{status_color}][blink]●[/blink] {status_text}[/{status_color}]  [dim]• {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )

        grid.add_row(
            Align.left(Text.from_markup(ASCII_ART.strip("\n")), vertical="middle"),
            Align.right(info_table, vertical="middle")
        )
        return Panel(grid, style="blue", padding=(1, 2))

    def generate_progress(self, progress: float, message: str, profiler_progress: float = 0.0):
        """Generate the main progress display with high-quality bars."""
        is_profiling = 0 < profiler_progress < 1.0
        
        progress_obj = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None, pulse_style="yellow"),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            expand=True
        )
        
        if is_profiling:
            task_id = progress_obj.add_task("[yellow]Learning Structure...", total=100)
            progress_obj.update(task_id, completed=profiler_progress * 100)
            title = "Initialization Phase"
            border_style = "yellow"
        else:
            task_id = progress_obj.add_task(f"[white]{message}", total=100)
            progress_obj.update(task_id, completed=progress)
            title = "Live Monitoring"
            border_style = "cyan"
            
        return Panel(
            progress_obj,
            title=title,
            border_style=border_style,
            padding=(1, 2)
        )
        
    def generate_stats(self, token_stats: dict):
        """Generate statistics panel."""
        table = Table(show_header=False, expand=True, box=None)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")
        
        # Calculate uptime
        uptime_sec = token_stats.get("uptime_seconds", 0)
        hours = uptime_sec // 3600
        minutes = (uptime_sec % 3600) // 60
        seconds = uptime_sec % 60
        if hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        elif minutes > 0:
            uptime_str = f"{minutes}m {seconds}s"
        else:
            uptime_str = f"{seconds}s"
            
        table.add_row("⏱️  Uptime", uptime_str)
        table.add_row("🧠 LLM Calls", str(token_stats.get("llm_calls", 0)))
        table.add_row("🎟️  Tokens", str(token_stats.get("total_tokens", 0)))
        table.add_row("💾 Cache Hits", f"{token_stats.get('cached_calls', 0)} ({token_stats.get('cache_hit_rate', 0)}%)")
        table.add_row("🎯 Patterns", f"{token_stats.get('pattern_matched', 0)} (+{token_stats.get('dynamic_patterns', 0)} dynamic)")
        
        # Add anomaly stats
        anomaly_stats = token_stats.get("anomaly_stats", {})
        table.add_row("📈 Frequency", f"{anomaly_stats.get('frequency', 0.0):.1f} L/min")
        table.add_row("🔍 Structures", str(anomaly_stats.get("known_structures", 0)))
        
        return Panel(
            Align.center(table, vertical="middle"),
            title="Statistics",
            border_style="yellow",
            padding=(1, 1)
        )
        
    def generate_logs(self, recent_logs: list):
        """Generate log tail panel."""
        log_text = Text()
        for log in recent_logs[-10:]:
            log_text.append(f"{log}\n")
            
        return Panel(
            log_text,
            title="Recent Activity",
            border_style="white",
            style="dim"
        )
        
    def generate_footer(self):
        """Generate footer with controls info."""
        return Panel(
            Align.center("[dim]Press [b]Ctrl+C[/b] to stop monitoring | [b]telewatch status[/b] to check remotely[/dim]"),
            style="blue"
        )

    def render(self, state, token_stats, recent_logs):
        """Update the entire layout."""
        # Calculate profiler progress
        # Assuming state might have profiler_progress or we pass it via token_stats for now
        profiler_progress = token_stats.get("profiler_progress", 0.0)
        
        self.layout["header"].update(self.generate_header(state.process_name, state.status, profiler_progress))
        self.layout["left"].update(self.generate_progress(state.progress, state.message, profiler_progress))
        self.layout["right"].update(self.generate_stats(token_stats)) 
        self.layout["footer"].update(self.generate_footer())
        
        return self.layout
