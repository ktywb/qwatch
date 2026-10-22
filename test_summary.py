"""Focused checks for completed-flow summaries and native Quartus reports."""

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from qwatch import ANSI_RE, ClockFmax, QWatch, QuartusReportReader, ReportSummary, Task


def flow(*, status: str = "done", success: int = 1) -> Task:
    return Task(1, "Flow", "run1", 100, status, 100, 200, 200, 100,
                1234, 0, 0, "done" if success else "failed", success, 1, 2, "host")


class CompletedSummaryTest(unittest.TestCase):
    def test_native_reports_are_read_only_for_current_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fit = root / "demo.fit.summary"
            fit.write_text(
                "Fitter Status : Successful\n"
                "Logic utilization (in ALMs) : 274,815 / 912,800 ( 30 % )\n"
                "Total dedicated logic registers : 613085\n"
                "Total RAM Blocks : 5,467 / 13,272 ( 41 % )\n"
                "Total DSP Blocks : 54 / 8,528 ( < 1 % )\n"
            )
            sta = root / "demo.sta.summary"
            sta.write_text(
                "Type  : Setup 'clock_a'\nSlack : -0.335\nTNS   : -3383.485\n"
                "Corner: Slow 100C\n"
                "Type  : Setup 'clock_b'\nSlack : 0.271\nTNS   : 0.000\n"
                "Type  : Hold 'clock_a'\nSlack : -8.000\nTNS   : -9999.000\n"
            )
            rpt = root / "demo.sta.rpt"
            rpt.write_text(
                "; Fmax Summary ;\n"
                "; Fmax ; Restricted Fmax ; Clock Name ; Note ; Worst-Case Operating Conditions ;\n"
                "; 358.29 MHz ; 300.00 MHz ; clock_a ; ; Slow 100C ;\n"
                "; 0.432 GHz ; 431000 kHz ; clock_b ; ; Slow 0C ;\n"
                "+-------------------+\n"
            )
            for path in (fit, sta, rpt):
                os.utime(path, (150, 150))
            reader = QuartusReportReader(root)
            summary = reader.read("demo", 100, 200)
            self.assertEqual(summary.resource_source, "FIT")
            self.assertEqual(summary.alms, "274,815 / 912,800 ( 30 % )")
            self.assertEqual(summary.setup_wns, -0.335)
            self.assertEqual(summary.worst_clock_tns, -3383.485)
            self.assertEqual(summary.setup_clock, "clock_a")
            self.assertEqual(summary.setup_corner, "Slow 100C")
            self.assertEqual(summary.hold_wns, -8.0)
            self.assertEqual(summary.fmax, (
                ClockFmax("clock_a", 358.29, 300.0, "Slow 100C"),
                ClockFmax("clock_b", 432.0, 431.0, "Slow 0C"),
            ))
            self.assertEqual(reader.read("demo", 100, 200), summary)

            # An existing report from the previous generation is not reused.
            stale = reader.read("demo", 300, 400)
            self.assertEqual(stale, ReportSummary())

    def test_report_table_fallback_and_synthesis_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo.syn.summary").write_text(
                "Synthesis Status : Successful\n"
                "Logic utilization estimate (in ALMs) : 100 / 200 ( 50 % )\n"
                "Estimated DSP Blocks Post-Merging : 4\n"
            )
            (root / "demo.sta.rpt").write_text(
                "; Setup Summary ;\n"
                "; Clock ; Slack ; End Point TNS ; Failing End Points ;\n"
                "; clock_a ; -0.500 ; -7.000 ; 3 ;\n"
                "; clock_b ; 0.300 ; 0.000 ; 0 ;\n"
                "+-------------------+\n"
                "; Hold Summary ;\n"
                "; Clock ; Slack ; End Point TNS ; Failing End Points ;\n"
                "; clock_a ; -0.125 ; -1.000 ; 2 ;\n"
                "+-------------------+\n"
                "; Fmax Summary ;\n"
                "; Fmax ; Restricted Fmax ; Clock Name ; Note ; Worst-Case Operating Conditions ;\n"
                "; 350.00 MHz ; 345.00 MHz ; clock_a ; ; Slow 100C ;\n"
                "+-------------------+\n"
            )
            for path in root.iterdir():
                os.utime(path, (150, 150))
            summary = QuartusReportReader(root).read("demo", 100, 200)
            self.assertEqual(summary.resource_source, "SYN estimate")
            self.assertEqual(summary.setup_wns, -0.5)
            self.assertEqual(summary.worst_clock_tns, -7.0)
            self.assertEqual(summary.setup_clock, "clock_a")
            self.assertEqual(summary.hold_wns, -0.125)
            self.assertEqual(summary.fmax[0].mhz, 350.0)
            self.assertEqual(summary.fmax[0].restricted_mhz, 345.0)

    def test_fmax_does_not_infer_missing_frequencies_or_read_other_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rpt = Path(temporary) / "demo.sta.rpt"
            rpt.write_text(
                "; Other Summary ;\n"
                "; Fmax ; Restricted Fmax ; Clock Name ;\n"
                "; 999.00 MHz ; 999.00 MHz ; not_a_summary_clock ;\n"
                "; Fmax Summary ;\n"
                "; Fmax ; Restricted Fmax ; Clock Name ;\n"
                "; N/A ; 300.00 MHz ; top|pll|clk ;\n"
                "; N/A ; N/A ; unreported_clock ;\n"
                "; 0 MHz ; -1 MHz ; invalid_clock ;\n"
                "+-------------------+\n"
                "; 999.00 MHz ; 999.00 MHz ; later_panel_clock ;\n"
            )
            self.assertEqual(QuartusReportReader.parse_fmax(rpt),
                             (ClockFmax("top|pll|clk", None, 300.0),))
            rpt.write_text(rpt.read_text().replace("300.00 MHz", "N/A"))
            self.assertEqual(QuartusReportReader.parse_fmax(rpt), ())

    def test_terminal_flow_replaces_processes_and_new_generation_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monitor = QWatch(Path(temporary), "demo", 1, None, 20)
            monitor.session_start = monitor.invocation_start = 100
            monitor.session_run_name = "run1"
            monitor.rows = [flow()]
            monitor.report_summary = ReportSummary(
                "FIT", "100 / 200 ( 50 % )", "1000", "20", "4", -0.5, -7.0,
                "clock_a", "Slow 100C", -0.125,
                (ClockFmax("clock_b", 200.0, 190.0, "Slow 0C"),
                 ClockFmax("clock_a", 350.0, 345.0, "Slow 100C"),
                 ClockFmax("clock_c", 400.0, 400.0, "Slow 100C"),
                 ClockFmax("clock_d", 500.0, 450.0, "Slow 0C")),
            )
            terminal = SimpleNamespace(size=lambda: os.terminal_size((110, 36)))
            rendered = io.StringIO()
            with contextlib.redirect_stdout(rendered):
                monitor.render(terminal)
            output = rendered.getvalue()
            self.assertIn("Flow: SUCCESS", output)
            self.assertIn("Summary", output)
            self.assertIn("Critical 2", output)
            self.assertIn("Worst clock TNS -7.000 ns", output)
            self.assertIn("Worst setup clock: clock_a", output)
            self.assertIn("Hold: WNS -0.125 ns", output)
            self.assertIn("Counts: Errors 1  Critical 2", output)
            self.assertIn("Resources (FIT):", output)
            self.assertIn("Fmax: 350.00 MHz  Restricted: 345.00 MHz", output)
            self.assertIn("worst setup clock; +3 clocks", output)
            self.assertNotIn("Processes", output)

            medium_terminal = SimpleNamespace(size=lambda: os.terminal_size((80, 34)))
            medium_rendered = io.StringIO()
            with contextlib.redirect_stdout(medium_rendered):
                monitor.render(medium_terminal)
            self.assertLessEqual(len(medium_rendered.getvalue().splitlines()), 34)
            self.assertNotIn("Events", medium_rendered.getvalue())

            short_terminal = SimpleNamespace(size=lambda: os.terminal_size((80, 24)))
            short_rendered = io.StringIO()
            with contextlib.redirect_stdout(short_rendered):
                monitor.render(short_terminal)
            self.assertLessEqual(len(short_rendered.getvalue().splitlines()), 24)
            self.assertIn("DSP 4", short_rendered.getvalue())
            self.assertIn("Fmax: 350.00 MHz", short_rendered.getvalue())

            monitor.messages.path = Path(temporary) / "demo.sta.qmsgdb"
            monitor.messages.history.extend(f"message {index}" for index in range(20))
            for width, height in ((80, 24), (80, 34), (120, 40), (120, 44), (160, 48)):
                tall_rendered = io.StringIO()
                with contextlib.redirect_stdout(tall_rendered):
                    monitor.render(SimpleNamespace(size=lambda: os.terminal_size((width, height))))
                tall_output = ANSI_RE.sub("", tall_rendered.getvalue())
                self.assertLessEqual(len(tall_output.splitlines()), height)
                self.assertTrue(all(len(line) <= width for line in tall_output.splitlines()))
                self.assertIn("↑/↓ scroll", tall_output)
                self.assertIn("message 19", tall_output)
                if height >= 44:
                    self.assertIn("Fmax by clock", tall_output)
                    self.assertIn("Restricted (MHz)", tall_output)
                if height >= 48:
                    self.assertIn("clock_d", tall_output)
                    self.assertIn("message 16", tall_output)

            monitor.begin_generation(300, "new Quartus process")
            self.assertIsNone(monitor.completed_flow())
            self.assertEqual(monitor.report_summary, ReportSummary())

            failed = Task(2, "Flow", "run1", 100, "done", 300, 400, 400, 100,
                          1235, 0, 0, "failed", 0, 1, 2, "host")
            monitor.adopt_rows([failed], failed)
            self.assertIn("FAILED", monitor.health_line(110))
            self.assertIn("Result: FAILED", "".join(monitor.summary_lines(failed, 110)))

            running = Task(3, "Flow", "run1", 0, "running", 500, 500, 0, 0,
                           1236, 0, 0, "", 0, 0, 0, "host")
            monitor.adopt_rows([running], running)
            self.assertIsNone(monitor.completed_flow())

    def test_full_compile_without_flow_row_waits_for_final_sta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monitor = QWatch(Path(temporary), "demo", 1, None, 20)
            synthesis = replace(flow(), name="Analysis & Synthesis", id=1,
                                start_time=100, end_time=150)
            fitter = replace(flow(), name="Fitter", id=2,
                             start_time=151, end_time=200)
            monitor.adopt_rows([synthesis, fitter], synthesis)
            self.assertIsNone(monitor.completed_flow())

            sta = replace(flow(), name="Timing Analysis (Finalize)", id=3,
                          start_time=210, end_time=230, critical_warnings=6)
            monitor.adopt_rows([synthesis, fitter, sta], synthesis)
            self.assertEqual(monitor.completed_flow(), sta)
            self.assertIn("Flow: SUCCESS", monitor.health_line(110))
            self.assertIn("Critical 6", "".join(monitor.summary_lines(sta, 110)))

            failed_sta = replace(sta, success=0, result="failed")
            monitor.adopt_rows([synthesis, fitter, failed_sta], synthesis)
            self.assertIn("Flow: FAILED", monitor.health_line(110))


if __name__ == "__main__":
    unittest.main()
