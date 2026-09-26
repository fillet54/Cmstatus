"""UI component library: macros, filters, marking config, style guide.  Run: python -m unittest discover -s tests"""
import os
import re
import shutil
import tempfile
import unittest

from flask import render_template_string

from cmtrack import create_app
from cmtrack.ui import iso, marking_from_env, utc

IMPORT = '{% import "ui/components.html" as ui %}'


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "TICKET_SOURCES": {},
                               "UI_MARKING": {"text": "CUI", "bg": "#502B85", "fg": "#FFFFFF"},
                               "UI_PROGRAM": "[PROGRAM]"})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def render(self, source, **ctx):
        with self.app.test_request_context():
            return render_template_string(IMPORT + source, **ctx)

    # ------------------------------------------------------------------ config and filters

    def test_marking_from_env(self):
        self.assertIsNone(marking_from_env(""))
        self.assertEqual(marking_from_env("CUI"), {"text": "CUI", "bg": "#502B85", "fg": "#FFFFFF"})
        self.assertEqual(marking_from_env("unclassified")["bg"], "#007A33")
        self.assertEqual(marking_from_env("SPECIAL", "#123456, #000000"),
                         {"text": "SPECIAL", "bg": "#123456", "fg": "#000000"})
        with self.assertRaises(ValueError):
            marking_from_env("SPECIAL")                      # no guessing colours for other markings

    def test_utc_and_iso(self):
        self.assertEqual(utc("2026-09-23 14:24:07"), "2026-09-23 14:24Z")           # SQLite datetime('now')
        self.assertEqual(utc("2026-09-23T10:24:07-04:00", seconds=True), "2026-09-23 14:24:07Z")
        self.assertEqual(utc("2027-03-15"), "2027-03-15")                           # dates stay dates
        self.assertEqual((utc(None), utc("")), ("", ""))
        self.assertEqual(iso("2026-09-23 14:24:07"), "2026-09-23T14:24:07Z")

    # ------------------------------------------------------------------ shell

    def test_layout_markings_and_nav(self):
        html = self.app.test_client().get("/ui").get_data(as_text=True)
        for pos in ("top", "bottom"):
            self.assertIn(f'class="ui-marking ui-marking--{pos}" role="note" aria-label="Marking: CUI"', html)
        self.assertIn("--ui-marking-bg: #502B85", html)
        self.assertIn("[PROGRAM]", html)
        self.assertIn('href="/backlogs"', html)
        self.assertIn('class="ui-skip" href="#main"', html)
        self.app.config["UI_MARKING"] = None
        self.assertIn("[Marking not configured]", self.app.test_client().get("/ui").get_data(as_text=True))
        self.app.config["UI_NAV"] = [("x", "Missing", "ui.nope"), ("cis", "CIs", "ui.cis")]
        html = self.app.test_client().get("/ui").get_data(as_text=True)
        self.assertNotIn("Missing", html)                                          # unknown endpoints skipped

    def test_styleguide_renders_everything(self):
        r = self.app.test_client().get("/ui")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for needle in ("ui-state--blocked", "ui-statebar__done", 'data-move="top"', "ui-rank-item--lifted",
                       "&lt;ul class=&#34;ui-tickets&#34;&gt;", "{% call ui.page_header"):
            self.assertIn(needle, html)

    # ------------------------------------------------------------------ macros

    def test_state_pill_and_bar(self):
        html = self.render('{{ ui.state_pill("blocked", title="Source status: Ready") }}')
        self.assertIn('class="ui-state ui-state--blocked" title="Source status: Ready"', html)
        self.assertIn("Blocked", html)
        self.assertIn("ui-state--error", self.render('{{ ui.state_pill("nonsense") }}'))   # unknown -> error
        progress = {"done": 2, "in_progress": 1, "error": 1, "total": 4}
        html = self.render("{{ ui.state_bar(p) }}", p=progress)
        widths = [float(w) for w in re.findall(r"width: ([\d.]+)%", html)]
        self.assertEqual((widths, sum(widths)), ([25.0, 50.0, 25.0], 100.0))     # workflow order: in_progress, done, error
        self.assertIn('aria-label="1 in progress, 2 done, 1 error"', html)
        self.assertIn('aria-label="no tickets"', self.render("{{ ui.state_bar(p) }}", p={"total": 0}))

    def test_state_reason_only_for_error_and_blocked(self):
        t = {"state": "blocked", "state_reason": "waits on NAVX-205"}
        self.assertIn("ui-reason--blocked", self.render("{{ ui.state_reason(t) }}", t=t))
        t["state"] = "in_progress"
        self.assertNotIn("waits", self.render("{{ ui.state_reason(t) }}", t=t))

    def test_buttons_escape_attrs_and_label_icons(self):
        html = self.render('{{ ui.button("Remove", variant="danger", attrs={"hx-post": "/x", "hx-confirm": "Remove \\"A\\"?"}) }}')
        self.assertIn('class="ui-btn ui-btn--danger"', html)
        self.assertIn('hx-post="/x"', html)
        self.assertIn('hx-confirm="Remove &#34;A&#34;?"', html)
        self.assertIn('href="/w"', self.render('{{ ui.button("Go", href="/w") }}'))
        html = self.render('{{ ui.icon_button("refresh", "Refresh from Jira") }}')
        self.assertIn('aria-label="Refresh from Jira"', html)
        self.assertIn('aria-hidden="true"', html)                                  # the svg itself is decorative

    def test_rank_item_keeps_drag_contract(self):
        t = {"key": "PRG-1", "summary": "S", "state": "done", "rank": "i", "cis": ["NAV-SW"]}
        html = self.render('{{ ui.rank_item(t, 3, remove_attrs={"hx-post": "/r"}) }}', t=t)
        for needle in ('draggable="true"', 'data-key="PRG-1"', 'data-rank="i"', 'data-state="done"', "data-pos>3<",
                       'data-move="top"', 'data-move="up"', 'data-move="down"', 'hx-post="/r"', "NAV-SW"):
            self.assertIn(needle, html)

    def test_escaping(self):
        html = self.render("{{ ui.ident(x) }}{{ ui.badge(x) }}{{ ui.chip(x) }}", x="<script>")
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
