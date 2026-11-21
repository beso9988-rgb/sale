import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Offline tests do not launch a browser or contact Telegram/Tazkarti.
try:
    import playwright.sync_api
except ImportError:
    browser = types.ModuleType('playwright.sync_api')
    browser.sync_playwright = None
    sys.modules['playwright'] = types.ModuleType('playwright')
    sys.modules['playwright.sync_api'] = browser

import monitor_runtime as runtime
import tazkarti_watch as monitor


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        monitor._OUTBOX = None
        monitor.API_STATE = {}

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()

    def test_default_poll(self):
        self.assertEqual(runtime.poll_seconds({}), 60)

    def test_fast_requires_ack(self):
        with self.assertRaises(ValueError):
            runtime.poll_seconds({'TARZKARTI_POLL_SECONDS': '10'})

    def test_fast_opt_in(self):
        self.assertEqual(runtime.poll_seconds({'TARZKARTI_POLL_SECONDS': '10', 'TARZKARTI_FAST_POLL_RISK_ACK': '1'}), 10)

    def test_fast_floor(self):
        with self.assertRaises(ValueError):
            runtime.poll_seconds({'TARZKARTI_POLL_SECONDS': '1', 'TARZKARTI_FAST_POLL_RISK_ACK': '1'})

    def test_cadence_deducts_work(self):
        with patch.object(runtime.time, 'monotonic', return_value=104), patch.object(runtime.random, 'uniform', return_value=0):
            self.assertEqual(runtime.cycle_delay(100, 10), 6)

    def test_error_backoff(self):
        with patch.object(runtime.time, 'monotonic', return_value=100), patch.object(runtime.random, 'uniform', return_value=0):
            self.assertEqual(runtime.cycle_delay(100, 10, 3), 80)
            self.assertEqual(runtime.cycle_delay(100, 60, 20), 900)

    def test_outbox_survives_failure_and_restart(self):
        box = runtime.DurableOutbox('queue.json')
        box.enqueue('alert')
        with self.assertRaises(OSError):
            box.flush(lambda _: (_ for _ in ()).throw(OSError()))
        restored = runtime.DurableOutbox('queue.json')
        sent = []
        restored.flush(sent.append)
        self.assertEqual(sent, ['alert'])
        self.assertEqual(json.loads(Path('queue.json').read_text()), [])

    def test_corrupt_queue_is_not_discarded(self):
        Path('queue.json').write_text('broken')
        with self.assertRaises(json.JSONDecodeError):
            runtime.DurableOutbox('queue.json')

    def test_atomic_json(self):
        runtime.atomic_json('state.json', {'value': 'قائمة'})
        self.assertEqual(json.loads(Path('state.json').read_text())['value'], 'قائمة')
        self.assertFalse(Path('state.json.tmp').exists())
        self.assertEqual(Path('state.json').stat().st_mode & 0o777, 0o600)

    def state(self):
        return {'baseline_done': True, 'avail': ['old'], 'avail_info': {'old': {'lines': ['A', 'B'], 'teams': ['A', 'B'], 'href': ''}}, 'removed_notified': {}}

    def test_unknown_scan_preserves_baseline(self):
        state = self.state()
        with patch.object(monitor, 'scan', return_value=('UNKNOWN', {})), patch.object(monitor, 'send_telegram') as send:
            monitor.process(types.SimpleNamespace(), state, False)
        self.assertEqual(state['avail'], ['old'])
        self.assertFalse(state['cycle_ok'])
        send.assert_not_called()

    def test_partial_multi_page_scan_preserves_baseline(self):
        state = self.state()
        with patch.object(monitor, 'PAGES', ['one', 'two']), patch.object(monitor, 'scan', side_effect=[('OK', {}), ('UNKNOWN', {})]):
            monitor.process(types.SimpleNamespace(), state, False)
        self.assertEqual(state['avail'], ['old'])

    def test_block_halts_without_clearing_state(self):
        state = self.state()
        with patch.object(monitor, 'scan', return_value=('BLOCKED', {})), patch.object(monitor, 'send_telegram'):
            monitor.process(types.SimpleNamespace(), state, False)
        self.assertEqual(state['avail'], ['old'])
        self.assertTrue(state['halt_reason'])

    def test_scan_exception_preserves_baseline(self):
        state = self.state()
        with patch.object(monitor, 'scan', side_effect=TimeoutError()):
            monitor.process(types.SimpleNamespace(), state, False)
        self.assertEqual(state['avail'], ['old'])
        self.assertFalse(state['cycle_ok'])

    def test_halted_start_makes_no_browser_calls(self):
        with patch.object(monitor, 'load_state', return_value={'halt_reason':'http_429'}), patch.object(monitor, 'sync_playwright') as browser:
            with self.assertRaises(SystemExit) as result:
                monitor.main()
        self.assertEqual(result.exception.code, 78)
        browser.assert_not_called()

    def test_public_report_never_confirms_tickets(self):
        info = {'lines': ['A vs B'], 'teams': ['A', 'B'], 'url': 'listing'}
        with patch.object(monitor, 'send_telegram') as send:
            monitor.send_availability_report({'match': info})
        self.assertIn('إشارات', send.call_args.args[0])

    def test_telegram_failure_does_not_log_token_or_lose_alert(self):
        monitor.send_telegram('pending')
        with patch.object(monitor, 'BOT_TOKEN', 'TEST_SECRET'), patch.object(monitor, 'CHAT_ID', '123'), patch.object(monitor.urllib.request, 'urlopen', side_effect=OSError('TEST_SECRET')), patch.object(monitor, 'log') as log:
            monitor.flush_telegram()
        self.assertEqual(monitor.get_outbox().items, ['pending'])
        self.assertNotIn('TEST_SECRET', str(log.call_args_list))

    def test_distinct_known_feed_updates_are_not_cooldown_suppressed(self):
        page = types.SimpleNamespace(on=lambda event, callback: setattr(page, 'callback', callback))
        monitor.attach_api_observer(page)
        def response(value):
            return types.SimpleNamespace(url='https://tazkarti.com/data/matches-list-json.json', headers={'content-type': 'application/json'}, status=200, body=lambda: json.dumps({'match': value}).encode())
        with patch.object(monitor, 'send_telegram') as send:
            page.callback(response(1))
            page.callback(response(2))
            page.callback(response(3))
            page.callback(response(3))
        self.assertEqual(send.call_count, 2)

    def test_all_teams_by_default(self):
        self.assertEqual(monitor.TEAMS_FILTER, [])


if __name__ == '__main__':
    unittest.main()
