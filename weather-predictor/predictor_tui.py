#!/usr/bin/env python3
"""Textual-based htop-style UI for the weather predictor.

Reuses all the data/probability logic from predictor.py; only the display
layer changes. Run with the venv python:

    ./venv/bin/python3 predictor_tui.py [STATION_ID]
"""
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog, Static

from predictor import (
    PR_TZ, POLL_SEC, SPARKS,
    Assertion, State, Snapshot, Station,
    fetch_station, build_snapshot, refresh_auto, eval_assertion,
    find_informative_bin, most_likely_max, movement_cents,
    parse_expr, log_snapshot, record_kalshi,
)
try:
    import calibration as _calibration
except Exception:
    _calibration = None


HELP_TEXT = """[bold]Comandos[/]
  [cyan]set 1 >89F[/]         fijar aserción 1
  [cyan]set 2 <75F[/]         fijar aserción 2
  [cyan]clear 1|2|3[/]        borrar aserción
  [cyan]mode floating|locked[/] auto-sugerida (slot 3)
  [cyan]station KJFK[/]       cambiar estación (resetea aserciones)
  [cyan]refresh[/] (o r)      forzar update ahora
  [cyan]calibration [all][/]  diagrama de confiabilidad
  [cyan]help[/] (o h)         esta ayuda
  [cyan]quit[/] (o q)         salir
"""


class WeatherApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 16; }
    #current, #forecast, #distribution { border: round cyan; padding: 0 1; width: 1fr; }
    #assertions { border: round magenta; height: 9; padding: 0 1; }
    #log { border: round blue; height: 1fr; padding: 0 1; }
    #cmd { dock: bottom; height: 3; border: round yellow; }
    """

    BINDINGS = [
        ("q", "quit", "Salir"),
        ("r", "refresh", "Refrescar"),
        ("h", "help", "Ayuda"),
    ]

    def __init__(self, station_id: str):
        super().__init__()
        self.state: State | None = None
        self.initial_station_id = station_id
        self._last_settle_day = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Static("cargando...", id="current")
            yield Static("", id="forecast")
            yield Static("", id="distribution")
        yield Static("", id="assertions")
        yield RichLog(id="log", max_lines=200, markup=True, wrap=False)
        yield Input(placeholder="comando (help)", id="cmd")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "Weather Predictor"
        self.sub_title = f"cargando {self.initial_station_id}..."
        try:
            station = fetch_station(self.initial_station_id)
        except Exception as e:
            self.query_one("#log", RichLog).write(
                f"[red]No se pudo cargar estación {self.initial_station_id}: {e}[/]")
            return
        self.state = State(station)
        self.sub_title = f"{station.id} — {station.name}"
        self.query_one("#log", RichLog).write(
            f"[green]✓[/] Cargada {station.id} [dim]({station.tz.key})[/]")
        self.refresh_data()
        self.set_interval(POLL_SEC, self.refresh_data)

    # ─── data refresh ───

    @work(thread=True, exclusive=True)
    def refresh_data(self) -> None:
        if self.state is None:
            return
        try:
            snap = build_snapshot(self.state.station)
        except Exception as e:
            self.call_from_thread(self._log, f"[red]Error en fetch:[/] {e}")
            return
        if _calibration is not None:
            today = datetime.now(self.state.station.tz).date()
            if self._last_settle_day != today:
                try:
                    _calibration.settle_pending(self.state.station)
                    self._last_settle_day = today
                except Exception as e:
                    self.call_from_thread(self._log,
                                          f"[yellow]settle error:[/] {e}")
        self.call_from_thread(self._on_snapshot, snap)

    def _on_snapshot(self, snap: Snapshot) -> None:
        assert self.state is not None
        self.state.last_snapshot = snap
        refresh_auto(self.state, snap)
        # also record history for each active user assertion
        for slot in (1, 2):
            a = self.state.assertions.get(slot)
            if a is not None:
                p, _ = eval_assertion(a, snap)
                a.history.append((snap.fetched_at, p))
        # auto-sugerida history is appended by refresh_auto via carried hist
        a3 = self.state.assertions.get(3)
        if a3 is not None:
            p, _ = eval_assertion(a3, snap)
            a3.history.append((snap.fetched_at, p))
        try:
            log_snapshot(snap, self.state.station, self.state.assertions)
        except Exception as e:
            self._log(f"[yellow]log CSV error:[/] {e}")
        try:
            record_kalshi(snap, self.state.station)
        except Exception as e:
            self._log(f"[yellow]kalshi error:[/] {e}")
        self._render(snap)
        self._log(f"[dim]{snap.station_local.strftime('%H:%M:%S')}[/] "
                  f"update ok — {snap.current_temp_f:.1f}°F, "
                  f"max {snap.today_max_obs:.1f}°F, {snap.peak_status}")

    # ─── rendering ───

    def _render(self, snap: Snapshot) -> None:
        assert self.state is not None
        station = self.state.station
        pr = snap.station_local.astimezone(PR_TZ)
        self.sub_title = (f"{station.id} — {station.name}  │  "
                          f"local {snap.station_local.strftime('%H:%M %Z')}  │  "
                          f"PR {pr.strftime('%H:%M')}")

        self.query_one("#current", Static).update(self._panel_current(snap))
        self.query_one("#forecast", Static).update(self._panel_forecast(snap))
        self.query_one("#distribution", Static).update(self._panel_distribution(snap))
        self.query_one("#assertions", Static).update(self._panel_assertions(snap))

    def _panel_current(self, snap: Snapshot) -> Table:
        t = Table.grid(padding=(0, 1))
        t.add_column(style="bold")
        t.add_column()
        obs_age = (datetime.now(timezone.utc) - snap.current_obs_time).total_seconds() / 60
        # Temp + heat index (only show HI if meaningfully different)
        temp_line = f"{snap.current_temp_f:.1f}°F [dim]({snap.current_desc})[/]"
        if snap.heat_index_f and snap.heat_index_f > snap.current_temp_f + 1:
            temp_line += f"  [yellow]sens {snap.heat_index_f:.0f}°F[/]"
        elif snap.wind_chill_f and snap.wind_chill_f < snap.current_temp_f - 1:
            temp_line += f"  [cyan]sens {snap.wind_chill_f:.0f}°F[/]"
        t.add_row("Temp", temp_line)
        # dewpoint + humidity
        if snap.humidity_pct is not None:
            dp = f"{snap.dewpoint_f:.0f}°F" if snap.dewpoint_f is not None else "?"
            t.add_row("Humedad", f"{snap.humidity_pct:.0f}%  [dim]dp {dp}[/]")
        # wind
        if snap.wind_mph is not None:
            wl = f"{snap.wind_mph:.0f} mph {snap.wind_dir_card or ''}"
            if snap.wind_gust_mph:
                wl += f" [dim]gust {snap.wind_gust_mph:.0f}[/]"
            t.add_row("Viento", wl)
        # pressure + trend
        if snap.pressure_inhg is not None:
            pl = f"{snap.pressure_inhg:.2f} inHg"
            if snap.pressure_trend_3h is not None:
                d = snap.pressure_trend_3h
                arrow = "↑" if d > 0.02 else "↓" if d < -0.02 else "→"
                color = "green" if d > 0.02 else "red" if d < -0.02 else "dim"
                pl += f"  [{color}]{arrow}{d:+.02f}/3h[/]"
            t.add_row("Presión", pl)
        t.add_row("Última obs",
                  f"{snap.current_obs_time.astimezone(self.state.station.tz).strftime('%H:%M')}"
                  f" [dim]({obs_age:.0f}m)[/]")
        t.add_row("Hoy obs",
                  f"min {snap.today_min_obs:.1f}°F / max {snap.today_max_obs:.1f}°F")
        style = ("bold green" if "confirmado" in snap.peak_status
                 else "yellow" if "meseta" in snap.peak_status
                 else "cyan")
        t.add_row("Pico",
                  Text.from_markup(f"[{style}]{snap.peak_status}[/] "
                                   f"[dim]P(sube)={snap.prob_rising*100:.0f}%[/]"))
        return t

    def _panel_forecast(self, snap: Snapshot) -> Table:
        t = Table(title="Pronóstico (ens GFS)", title_style="bold", expand=True)
        t.add_column("hora", width=6)
        t.add_column("med", justify="right", width=5)
        t.add_column("p10-p90", justify="right", style="dim")
        for ts, med, p10, p90 in snap.forecast_next_hours:
            t.add_row(ts.strftime("%H:%M"),
                      f"{med:.0f}°F",
                      f"{p10:.0f}–{p90:.0f}°F")
        return t

    def _panel_distribution(self, snap: Snapshot) -> Text:
        dist = sorted(snap.ensemble_daily_maxes)
        n = len(dist)
        med = dist[n // 2]
        p10, p90 = dist[int(n * 0.1)], dist[int(n * 0.9)]
        spread = max(dist) - min(dist)
        val, width, p = find_informative_bin(snap.ensemble_daily_maxes)
        if spread < 0.1:
            mlv_line = Text.from_markup(
                f"[bold yellow]final = {med:.1f}°F[/] [dim](convergido)[/]")
        else:
            mlv_line = Text.from_markup(
                f"más probable: [bold yellow]{val:.1f}°F ±{width/2:.2f}[/] "
                f"(P={p*100:.0f}%)")
        txt = Text.from_markup("[bold]Max hoy (ensemble)[/]\n")
        txt.append(f"  mediana {med:.1f}°F\n")
        txt.append(f"  p10-p90 {p10:.1f}–{p90:.1f}°F\n")
        txt.append("  ")
        txt.append(mlv_line)
        # climatology context
        c = snap.climatology
        if c is not None and snap.climatology_target_f is not None:
            pct = c.percentile
            if pct >= 95: anom_style, anom_word = "red", "MUY CALIENTE"
            elif pct >= 80: anom_style, anom_word = "yellow", "caliente"
            elif pct >= 20: anom_style, anom_word = "green", "normal"
            elif pct >= 5: anom_style, anom_word = "cyan", "fresco"
            else: anom_style, anom_word = "bold cyan", "MUY FRÍO"
            txt.append("\n")
            txt.append(Text.from_markup(
                f"[bold]Climatología[/] [dim]{c.year_span}[/]\n"))
            txt.append(Text.from_markup(
                f"  {snap.climatology_target_f:.1f}°F = "
                f"[{anom_style}]p{pct:.0f}  {anom_word}[/]\n"))
            txt.append(f"  normal p50 {c.p50:.0f}°F  record {c.record:.0f}°F")
        return txt

    def _panel_assertions(self, snap: Snapshot) -> Table:
        assert self.state is not None
        t = Table(title=f"Aserciones [dim](auto modo: {self.state.auto_mode})[/]",
                  title_style="bold", expand=True)
        t.add_column("#", width=3)
        t.add_column("aserción", width=22)
        t.add_column("¢", justify="right", width=5)
        t.add_column("Δ", justify="right", width=7)
        t.add_column("estado", width=14)
        t.add_column("evolución", style="dim")
        for slot in (1, 2, 3):
            a = self.state.assertions.get(slot)
            if a is None:
                t.add_row(str(slot), "[dim]—[/]", "", "", "", "")
                continue
            prob, status = eval_assertion(a, snap)
            label = a.expr + ("  [dim](auto)[/]" if a.auto else "")
            cents = int(round(prob * 100))
            color = ("green" if status == "LIVE" and prob >= 0.5 else
                     "red" if "FALLIDA" in status else
                     "bold green" if "RESUELTA" in status else
                     "yellow")
            mv = movement_cents(a)
            if mv is None:
                mv_str = "[dim]—[/]"
            elif mv > 0:
                mv_str = f"[green]↑+{mv}¢[/]"
            elif mv < 0:
                mv_str = f"[red]↓{mv}¢[/]"
            else:
                mv_str = "[dim]→0[/]"
            spark = _sparkline([p for _, p in a.history])
            t.add_row(str(slot), label, f"[{color}]{cents:3d}¢[/]",
                      mv_str, status, spark)
        return t

    # ─── actions ───

    def action_refresh(self) -> None:
        self._log("[dim]refresh manual...[/]")
        self.refresh_data()

    def action_help(self) -> None:
        self._log(HELP_TEXT)

    # ─── command input ───

    @on(Input.Submitted, "#cmd")
    def on_cmd(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        self._log(f"[dim]»[/] {line}")
        self._handle_command(line)

    def _handle_command(self, line: str) -> None:
        if self.state is None:
            self._log("[red]estado no listo[/]")
            return
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                self.exit()
            elif cmd in ("h", "help"):
                self._log(HELP_TEXT)
            elif cmd in ("r", "refresh", "show"):
                self.refresh_data()
            elif cmd == "set":
                slot = int(parts[1])
                if slot not in (1, 2):
                    self._log("[red]slot 1 o 2 (slot 3 es auto)[/]")
                    return
                op, thr, half, expr = parse_expr(" ".join(parts[2:]))
                prev = self.state.assertions.get(slot)
                self.state.assertions[slot] = Assertion(
                    expr=expr, op=op, threshold=thr, bin_half=half,
                    history=prev.history if prev else [])
                self._log(f"[green]✓[/] aserción {slot}: {expr}")
                if self.state.last_snapshot:
                    self._render(self.state.last_snapshot)
            elif cmd == "clear":
                slot = int(parts[1])
                if slot == 3:
                    self._log("[yellow]slot 3 es auto; usa 'mode'[/]")
                elif slot in self.state.assertions:
                    del self.state.assertions[slot]
                    self._log(f"[green]✓[/] borrada slot {slot}")
                    if self.state.last_snapshot:
                        self._render(self.state.last_snapshot)
            elif cmd == "mode":
                m = parts[1].lower()
                if m not in ("floating", "locked"):
                    self._log("[red]floating o locked[/]")
                    return
                self.state.auto_mode = m
                if self.state.last_snapshot:
                    refresh_auto(self.state, self.state.last_snapshot)
                    self._render(self.state.last_snapshot)
                self._log(f"[green]✓[/] modo auto: {m}")
            elif cmd == "station":
                sid = parts[1].upper()
                try:
                    new = fetch_station(sid)
                except Exception as e:
                    self._log(f"[red]estación no encontrada: {e}[/]")
                    return
                self.state.set_station(new)
                self.sub_title = f"{new.id} — {new.name}"
                self._log(f"[green]✓[/] estación {new.id} — {new.name}")
                self.refresh_data()
            elif cmd == "calibration":
                self._show_calibration(parts)
            else:
                self._log(f"[yellow]comando desconocido: {cmd}[/] (help)")
        except Exception as e:
            self._log(f"[red]error:[/] {e}")

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _show_calibration(self, parts: list) -> None:
        if _calibration is None:
            self._log("[red]módulo calibration no disponible[/]")
            return
        scope = None if len(parts) > 1 and parts[1].lower() == "all" \
            else self.state.station.id
        rep = _calibration.reliability(scope)
        label = scope if scope else "todas"
        if rep.settled_n == 0:
            self._log(f"[yellow]sin datos resueltos aún ({label}); "
                      f"total snapshots={rep.total_n}[/]")
            return
        self._log(f"[bold]Reliability — {label}[/]  "
                  f"n={rep.settled_n}  Brier={rep.brier:.4f}")
        for b in rep.buckets:
            if b.n == 0:
                continue
            pos_hit = int(round(b.hit_rate * 20))
            pos_exp = int(round(b.mean_pred * 20))
            bar = ["·"] * 21
            bar[pos_exp] = "|"
            mark = "█" if b.hit_rate >= b.mean_pred else "▓"
            bar[pos_hit] = mark
            self._log(f"  {b.low:.1f}-{b.high:.1f}  n={b.n:3d}  "
                      f"pred={b.mean_pred*100:5.1f}%  "
                      f"hit={b.hit_rate*100:5.1f}%  "
                      f"[cyan]{''.join(bar)}[/]")


def _sparkline(values, width=24):
    if not values:
        return ""
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARKS[3] * len(vals)
    return "".join(SPARKS[min(len(SPARKS) - 1,
                              int((v - lo) / (hi - lo) * (len(SPARKS) - 1)))]
                   for v in vals)


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "KPHX"
    app = WeatherApp(sid)
    app.run()


if __name__ == "__main__":
    main()
