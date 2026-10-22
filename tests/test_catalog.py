import copy
import json
from pathlib import Path
import tempfile
import unittest

from cuddler_plugin.cuddler.catalog import CACHE_NAME, MAX_CATALOG_BYTES, CatalogStore, parse
from cuddler_plugin.cuddler.errors import CuddlerError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "catalog.json"


def live_document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def first_video(document, test):
    """The first enabled video entry that passes `test`; the recorded catalog decides which."""
    return next(entry for entry in document["models"]
                if entry.get("kind", "video") == "video" and entry.get("enabled") and test(entry))


class ParseTest(unittest.TestCase):
    def test_the_recorded_catalog_is_fully_supported(self):
        document = live_document()
        catalog = parse(document)
        self.assertEqual(catalog.problems(), [])
        enabled = [entry["id"] for entry in document["models"] if entry.get("enabled") is True]
        self.assertEqual(sorted(catalog.models), sorted(enabled))
        for kind in ("video", "image", "speech", "music"):
            self.assertTrue(catalog.of_kind(kind), kind)
        legacy = [entry["id"] for entry in document["models"] if "kind" not in entry]
        self.assertTrue(all(catalog.models[model]["kind"] == "video" for model in legacy))
        speech = catalog.of_kind("speech")[0]["id"]
        self.assertTrue(catalog.voices.get(speech))

    def test_unknown_kinds_and_fields_leave_only_that_model_out(self):
        document = live_document()
        document["models"].append({"object": "model", "id": "hologram-1", "kind": "hologram",
                                   "display_name": "Hologram", "enabled": True})
        changed = first_video(document, lambda entry: entry.get("request_fields"))
        changed["request_fields"] = changed["request_fields"] + ["teleport"]
        catalog = parse(document)
        self.assertNotIn("hologram-1", catalog.models)
        self.assertNotIn(changed["id"], catalog.models)
        self.assertEqual(len(catalog.models), len([e for e in document["models"] if e.get("enabled")]) - 2)
        self.assertEqual(sorted(catalog.problems()),
                         sorted(["hologram-1: unknown_kind", f"{changed['id']}: unknown_field"]))

    def test_unknown_modes_drop_only_that_usage(self):
        document = live_document()
        changed = first_video(document, lambda entry: len(entry.get("choices", [])) >= 2)
        changed["choices"].append({"mode": "time-travel", "variant": "default"})
        catalog = parse(document)
        modes = {choice["mode"] for choice in catalog.models[changed["id"]]["choices"]}
        self.assertNotIn("time-travel", modes)
        self.assertEqual(catalog.problems(), [f"{changed['id']}: time-travel: unknown_mode"])

    def test_disabled_models_are_hidden_without_being_a_problem(self):
        document = live_document()
        document["models"][0]["enabled"] = False
        catalog = parse(document)
        self.assertNotIn(document["models"][0]["id"], catalog.models)
        self.assertEqual(catalog.problems(), [])

    def test_malformed_documents_are_refused(self):
        for document in (None, [], {"object": "list", "models": []}, {"object": "catalog"}):
            with self.subTest(document=document), self.assertRaises(CuddlerError):
                parse(document)

    def test_a_value_of_the_wrong_type_leaves_only_that_model_out(self):
        base = live_document()
        target = first_video(base, lambda entry: entry.get("request_fields") and len(entry.get("choices", [])) >= 2)
        total = len([entry for entry in base["models"] if entry.get("enabled")])

        def put(**changes):
            return lambda entry: entry.update(changes)

        cases = {
            "kind is a list": (put(kind=["video"]), "unknown_kind"),
            "kind is an object": (put(kind={}), "unknown_kind"),
            "a field is a list": (put(request_fields=[["seed"]]), "unknown_field"),
            "a field is an object": (put(request_fields=[{}]), "unknown_field"),
            "fields are one string": (put(request_fields="seed"), "unknown_field"),
            "a limit is a float": (put(max_duration=30.5), "invalid_entry"),
            "choices is an object": (put(choices={}), "invalid_entry"),
            "a choice is a number": (lambda entry: entry["choices"].append(7), "invalid_entry"),
        }
        for label, (change, reason) in cases.items():
            with self.subTest(label):
                document = copy.deepcopy(base)
                change(next(entry for entry in document["models"] if entry["id"] == target["id"]))
                catalog = parse(document)
                self.assertNotIn(target["id"], catalog.models)
                self.assertEqual(len(catalog.models), total - 1)
                self.assertEqual(catalog.problems(), [f"{target['id']}: {reason}"])

    def test_a_mode_of_the_wrong_type_drops_only_that_usage(self):
        base = live_document()
        target = first_video(base, lambda entry: len(entry.get("choices", [])) >= 2)
        total = len([entry for entry in base["models"] if entry.get("enabled")])
        for label, value in (("a list", []), ("an object", {}), ("a number", 7)):
            with self.subTest(label):
                document = copy.deepcopy(base)
                next(entry for entry in document["models"] if entry["id"] == target["id"])["choices"].append(
                    {"mode": value, "variant": "default"})
                catalog = parse(document)
                self.assertEqual(len(catalog.models), total)
                self.assertEqual(catalog.problems(), [f"{target['id']}: {value}: unknown_mode"])

    def test_no_single_wrong_value_can_make_parse_raise(self):
        wrong = (None, True, -1, 1.5, 10 ** 30, "", "x", [], [[]], [{}], {}, {"a": [1]})
        failures = []
        for entry in live_document()["models"]:
            paths = [(name,) for name in entry]
            for name in ("choices", "resolutions", "aspect_ratios", "request_fields", "pricing"):
                if isinstance(entry.get(name), list) and entry[name]:
                    paths.append((name, 0))
                    if isinstance(entry[name][0], dict):
                        paths.extend((name, 0, sub) for sub in entry[name][0])
            for path in paths:
                for value in wrong:
                    changed = copy.deepcopy(entry)
                    target = changed
                    for step in path[:-1]:
                        target = target[step]
                    target[path[-1]] = value
                    try:
                        parse({"object": "catalog", "models": [changed]})
                    except Exception as error:  # the point of the test is that nothing escapes
                        failures.append(f"{entry['id']}.{'.'.join(map(str, path))}={value!r}: {type(error).__name__}")
        self.assertEqual(failures[:8], [], f"{len(failures)} wrong values raised")


class StoreTest(unittest.TestCase):
    def test_release_models_remain_available_offline_on_first_start(self):
        snapshot = Path(__file__).resolve().parents[1] / "data" / "catalog_snapshot.json"
        with tempfile.TemporaryDirectory() as directory:
            def offline():
                raise CuddlerError("network_error")

            store = CatalogStore(snapshot, Path(directory) / "no-cache", offline)
            catalog = store.current()
            self.assertEqual(catalog.source, "snapshot")
            self.assertEqual(catalog.problems(), [])
            release_models = {"minimax-speech-2.8-hd", "minimax-speech-2.8-turbo",
                              "eleven-music-2.5", "ideogram-4.5", "runway-gen4-turbo",
                              "runway-gen4.5", "seedream-5-pro"}
            self.assertTrue(release_models.issubset(catalog.models),
                            f"missing release models: {sorted(release_models - catalog.models.keys())}")
            for model in catalog.of_kind("speech"):
                self.assertTrue(catalog.voices.get(model["id"]), model["id"])
            with self.assertRaises(CuddlerError):
                store.refresh()
            self.assertEqual(store.current(), catalog)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.snapshot = self.dir / "snapshot.json"
        self.snapshot.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        self.fetched = live_document()
        self.store = CatalogStore(self.snapshot, self.dir / "private", lambda: copy.deepcopy(self.fetched))

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_the_snapshot_until_a_refresh_is_cached(self):
        self.assertEqual(self.store.current().source, "snapshot")
        self.assertTrue(self.store.refresh())
        self.assertFalse(self.store.refresh())  # unchanged
        self.assertEqual(self.store.current().source, "snapshot")  # fixed for this process
        later = CatalogStore(self.snapshot, self.dir / "private", lambda: self.fetched)
        self.assertEqual(later.current().source, "cache")

    def test_a_bad_refresh_keeps_the_previous_copy(self):
        self.fetched = {"object": "catalog", "models": []}
        with self.assertRaises(CuddlerError):
            self.store.refresh()
        self.assertFalse((self.dir / "private" / CACHE_NAME).exists())

    def test_an_unreadable_cache_falls_back_to_the_snapshot(self):
        (self.dir / "private").mkdir()
        (self.dir / "private" / CACHE_NAME).write_text("{broken")
        with self.assertLogs("cuddler", level="WARNING"):
            self.assertEqual(self.store.current().source, "snapshot")

    def test_no_copy_at_all_gives_an_empty_catalog(self):
        self.snapshot.unlink()
        self.assertEqual(self.store.current().source, "empty")
        self.assertEqual(self.store.current().models, {})


    def test_unusable_copies_never_raise_out_of_current(self):
        unusable = {
            "deeply nested": b"[" * 200_000 + b"]" * 200_000,
            "not an object": b"[]",
            "models is not a list": b'{"object": "catalog", "models": {}}',
            "not UTF-8": b"\xff\xfe{",
            "over the size limit": b" " * (MAX_CATALOG_BYTES + 1),
        }
        (self.dir / "private").mkdir()
        for label, blob in unusable.items():
            with self.subTest("cache: " + label):
                (self.dir / "private" / CACHE_NAME).write_bytes(blob)
                store = CatalogStore(self.snapshot, self.dir / "private", lambda: self.fetched)
                with self.assertLogs("cuddler", level="WARNING"):
                    self.assertEqual(store.current().source, "snapshot")
            with self.subTest("snapshot: " + label):
                broken = self.dir / "broken-snapshot.json"
                broken.write_bytes(blob)
                store = CatalogStore(broken, self.dir / "missing", lambda: self.fetched)
                with self.assertLogs("cuddler", level="WARNING"):
                    self.assertEqual(store.current().source, "empty")

    def test_refresh_stores_any_text_it_can_read_back(self):
        first = self.fetched["models"][0]
        first["display_name"] = "Clip \ud83d \u6a21\u578b"  # a lone surrogate and CJK text
        self.assertTrue(self.store.refresh())
        later = CatalogStore(self.snapshot, self.dir / "private", lambda: self.fetched)
        self.assertEqual(later.current().source, "cache")
        self.assertEqual(later.current().models[first["id"]]["display_name"], first["display_name"])

    def test_refresh_refuses_a_copy_the_next_start_could_not_load(self):
        self.fetched["models"][0]["display_name"] = "x" * MAX_CATALOG_BYTES
        with self.assertRaises(CuddlerError):
            self.store.refresh()
        self.assertFalse((self.dir / "private" / CACHE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
