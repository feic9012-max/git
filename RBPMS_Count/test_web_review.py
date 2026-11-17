import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import cv2
import numpy as np

from rbpms_web import Conflict, Project


def png(image):
    return cv2.imencode(".png", image)[1].tobytes()


class ReviewTest(unittest.TestCase):
    def test_reimport_corrects_empty_wrong_channel(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Project(Path(directory))
            image = np.zeros((128, 128, 3), np.uint8)
            cv2.circle(image, (45, 50), 12, (0, 0, 220), -1)
            original = png(image)
            image_id, _ = project.ingest("red.png", original, channel="green")
            project.review(image_id, dict(revision=0, action="edit", x=90, y=90, mode="add"))
            with self.assertRaises(Conflict):
                project.ingest("red.png", original, channel="red")
            project.review(image_id, dict(revision=1, action="edit", x=90, y=90, mode="remove"))
            self.assertEqual(project.ingest("red.png", original, channel="auto"), (image_id, True))
            state = project.review(image_id, contours=True)
            self.assertEqual((state["channel"], state["revision"], len(state["included"])), ("red", 3, 1))
            self.assertFalse(state["canUndo"])
            self.assertEqual(len(project.summaries()), 1)
            with project.connect() as db:
                actions = [row["action"] for row in db.execute("SELECT action FROM audit ORDER BY seq")]
            self.assertEqual(actions, ["edit", "edit", "correct_channel"])

    def test_red_channel_and_batch_summary(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Project(Path(directory))
            image = np.zeros((128, 128, 3), np.uint8)
            cv2.circle(image, (45, 50), 12, (0, 0, 220), -1)
            cv2.circle(image, (90, 80), 12, (255, 0, 0), -1)
            original = png(image)
            name = "视网膜_c1.png"
            image_id, created = project.ingest(name, original, channel="auto",
                                               image_path="样本/左眼/" + name)
            self.assertTrue(created)
            state = project.review(image_id, contours=True)
            self.assertEqual(state["channel"], "red")
            self.assertEqual(len(state["cells"]), 1)
            self.assertAlmostEqual(state["cells"][0]["x"], 45, delta=1)
            self.assertEqual(len(state["included"]), 1)
            self.assertEqual(project.ingest(name, original, channel="red"), (image_id, False))
            with self.assertRaises(Conflict):
                project.ingest(name, original, channel="green")
            project.review(image_id, dict(revision=0, action="edit", x=45, y=50, mode="remove"))
            summary = project.summaries()[0]
            self.assertEqual(summary["image_path"], "样本/左眼/" + name)
            self.assertEqual((summary["auto_count"], summary["final_count"]), (1, 0))

            def exported():
                return list(csv.reader(io.StringIO(Project(Path(directory)).summary_csv(image_id).decode("utf-8-sig"))))

            header = ["文件夹名", "自动计数", "人工审核后计数"]
            self.assertEqual(exported(), [header, ["左眼", "1", ""]])
            completed = project.review(image_id, dict(revision=1, action="complete"))
            self.assertEqual(completed["reviewed_count"], 0)
            self.assertEqual(exported(), [header, ["左眼", "1", "0"]])
            project.review(image_id, dict(revision=2, action="undo"))
            self.assertEqual(exported(), [header, ["左眼", "1", ""]])

    def test_round_trip(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory)
            project = Project(path)
            original = np.zeros((1024, 1024, 3), np.uint8)
            original[180:221, 180:221, 1] = 200
            labels = np.zeros(original.shape[:2], np.uint16)
            labels[180:221, 180:221] = 1
            cells = b"id,x_px,y_px,class\n1,200,200,strong_candidate\n"
            image_id, created = project.ingest("视网膜.png", png(original), png(labels), cells)
            self.assertTrue(created)
            self.assertEqual(project.ingest("重复.png", png(original)), (image_id, False))
            original[0, 0, 1] = 1
            self.assertNotEqual(project.ingest("视网膜.png", png(original), png(labels), cells)[0], image_id)
            self.assertEqual(project.review(image_id, contours=True)["cells"][0]["x"], 200)
            self.assertEqual(project.review(image_id)["included"], [1])

            def edit(revision, action, **detail):
                return project.review(image_id, dict(revision=revision, action=action, **detail))

            self.assertEqual(edit(0, "edit", x=400, y=600, mode="add")["added"], {"M0001": [400, 600]})
            self.assertEqual(edit(1, "edit", x=401, y=600, mode="add")["revision"], 1)
            self.assertEqual(edit(1, "edit", x=200, y=200, mode="remove")["included"], [])
            self.assertEqual(edit(2, "undo")["included"], [1])
            self.assertEqual(edit(3, "redo")["included"], [])
            with self.assertRaises(Conflict):
                edit(0, "undo")
            project = Project(path)
            restored = project.review(image_id)
            self.assertEqual(restored["added"], {"M0001": [400, 600]})
            self.assertEqual(restored["decisions"], {"1": False})
            data = project.review(image_id, export="csv")
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
            self.assertEqual(rows[0]["image_name"], "视网膜.png")
            self.assertEqual(rows[0]["class"], "strong_candidate")
            self.assertEqual(rows[0]["review_status"], "人工排除")
            self.assertEqual(rows[1]["x_px"], "400")
            overlay = project.review(image_id, export="png")
            self.assertEqual(cv2.imdecode(np.frombuffer(overlay, np.uint8), 1).shape, original.shape)

    def test_real_v3_generation(self):
        root = Path(__file__).parent
        image = next((root / "images").glob("*_c1.png"))
        expected = next((root / "results_v3").glob("*_cells.csv"))
        with expected.open(encoding="utf-8-sig", newline="") as file:
            reference = list(csv.DictReader(file))
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            project = Project(Path(directory))
            image_id, _ = project.ingest(image.name, image.read_bytes())
            state = project.review(image_id, contours=True)
            self.assertEqual(len(state["cells"]), len(reference))
            self.assertEqual(len(state["included"]), sum(row["class"] == "strong_candidate" for row in reference))


if __name__ == "__main__":
    unittest.main()

