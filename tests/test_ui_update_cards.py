#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for update progress cards behavior in ui.html.
Ensures that update cards:
1. Do not automatically display during silent background/startup checks.
2. Only display automatically when an update is genuinely detected.
3. Display when user triggers a manual update check.
"""
import os
import re
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(BASE_DIR, "ui.html")


class TestUIUpdateCards(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(UI_PATH), f"ui.html not found at {UI_PATH}")
        with open(UI_PATH, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_relay_progress_card_display_conditions(self):
        # Relay card should require isUpdating, isManual, or hasNewUpdate (not checking alone)
        self.assertIn("let _relayManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'fetching', 'applying'].includes(state);", self.html)
        self.assertIn("const isManual = !!_relayManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_cpa_progress_card_display_conditions(self):
        # CPA card should require isUpdating, isManual, or hasNewUpdate (not checking alone)
        self.assertIn("let _cpaManualCheck = false;", self.html)
        self.assertIn("const isUpdating = ['queued', 'downloading', 'verifying', 'staged', 'installing', 'rolling_back'].includes(state);", self.html)
        self.assertIn("const isManual = !!_cpaManualCheck;", self.html)
        self.assertIn("const shouldShow = isUpdating || isManual || hasNewUpdate;", self.html)

    def test_antigravity_progress_card_display_conditions(self):
        # Antigravity card should require isManual or hasNewUpdate (not checking alone)
        self.assertIn("let _antigravityManualCheck = false;", self.html)
        self.assertIn("const isManual = !!_antigravityManualCheck;", self.html)
        self.assertIn("const shouldShow = isManual || hasNewUpdate;", self.html)

    def test_auto_dismiss_timer_requires_shown_card(self):
        # Auto-dismiss timer should only attach if card currently has .show class
        # (avoiding silent auto-checks from creating/cancelling timers or popping up)
        self.assertIn("if (card.classList.contains('show'))", self.html)

    def test_manual_check_triggers_exist(self):
        # All three components must have manual check triggers
        self.assertIn('id="btn-relay-update"', self.html)
        self.assertIn('id="cpa-check-update"', self.html)
        self.assertIn('id="antigravity-check-update"', self.html)

        # Event listeners must set manual check flag and show the card
        self.assertIn("_relayManualCheck = true;", self.html)
        self.assertIn("_cpaManualCheck = true;", self.html)
        self.assertIn("_antigravityManualCheck = true;", self.html)


if __name__ == "__main__":
    unittest.main()
