"""Unit tests for bounded CSV/XLSX baseline Asset parsing."""

from __future__ import annotations

import csv
import io
import unittest
from uuid import UUID

from openpyxl import Workbook

from ams.application.asset_imports import (
    MAX_IMPORT_FILE_SIZE_BYTES,
    AssetImportFile,
    UnsupportedImportFileError,
)
from ams.infrastructure.asset_import_parser import (
    TEMPLATE_COLUMNS,
    CsvXlsxAssetImportParser,
)

CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002301")
TEAM_ID = UUID("00000000-0000-4000-8000-000000002302")
USER_ID = UUID("00000000-0000-4000-8000-000000002303")


class AssetImportParserTests(unittest.TestCase):
    """Verify template, scalar, encoding, type, size, and workbook validation."""

    def setUp(self) -> None:
        """Create the stateless production parser."""

        self.parser = CsvXlsxAssetImportParser()

    def test_csv_and_xlsx_parse_the_same_valid_row(self) -> None:
        """Both approved formats normalize one template row identically."""

        values: list[object] = [
            "Camera",
            "Club camera",
            "DONATION",
            2,
            str(CATEGORY_ID),
            str(TEAM_ID),
            str(USER_ID),
            "GOOD",
            "New",
        ]
        csv_row = self.parser.parse(self._csv([values]))[0]
        xlsx_row = self.parser.parse(self._xlsx([values]))[0]

        self.assertTrue(csv_row.valid)
        self.assertEqual(csv_row.data, xlsx_row.data)
        assert csv_row.data is not None
        self.assertEqual(csv_row.data.total_quantity, 2)
        self.assertEqual(csv_row.data.responsible_team_id, TEAM_ID)

    def test_invalid_row_returns_all_field_errors_and_blank_rows_are_skipped(self) -> None:
        """Bad scalar values remain reviewable and do not abort other rows."""

        source = self._csv(
            [
                ["", "x" * 1001, "", "1.5", "bad", "bad", "", "broken", "x" * 1001],
                [None] * len(TEMPLATE_COLUMNS),
            ]
        )
        row = self.parser.parse(source)[0]

        self.assertFalse(row.valid)
        self.assertEqual(row.row_number, 2)
        self.assertGreaterEqual(len(row.errors), 8)

    def test_optional_values_and_default_condition_are_supported(self) -> None:
        """Nullable Team/text and blank condition normalize to approved defaults."""

        row = self.parser.parse(
            self._csv(
                [
                    [
                        "Tripod",
                        "",
                        "PURCHASE",
                        "1",
                        str(CATEGORY_ID),
                        "",
                        str(USER_ID),
                        "",
                        "",
                    ]
                ]
            )
        )[0]
        assert row.data is not None
        self.assertIsNone(row.data.description)
        self.assertIsNone(row.data.responsible_team_id)
        self.assertEqual(row.data.condition.value, "GOOD")

    def test_unsupported_empty_oversized_bad_header_and_empty_data_are_rejected(self) -> None:
        """File-level failures use one stable unsupported-file exception."""

        sources = (
            AssetImportFile("data.txt", "text/plain", b"x"),
            AssetImportFile("data.csv", "text/csv", b""),
            AssetImportFile("data.csv", "text/csv", b"x" * (MAX_IMPORT_FILE_SIZE_BYTES + 1)),
            AssetImportFile("data.csv", "text/csv", b"bad,header\n1,2\n"),
            AssetImportFile(
                "data.csv",
                "text/csv",
                (",".join(TEMPLATE_COLUMNS) + "\n").encode(),
            ),
            AssetImportFile("data.csv", "text/csv", b"\xff"),
            AssetImportFile("data.xlsx", "application/octet-stream", b"bad"),
            AssetImportFile(
                "data.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"bad",
            ),
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(UnsupportedImportFileError):
                self.parser.parse(source)

    def test_row_limit_is_enforced(self) -> None:
        """A template cannot exceed the bounded 1000-row preview."""

        valid: list[object] = [
            "Camera",
            "",
            "DONATION",
            "1",
            str(CATEGORY_ID),
            "",
            str(USER_ID),
            "GOOD",
            "",
        ]
        with self.assertRaises(UnsupportedImportFileError):
            self.parser.parse(self._csv([valid] * 1001))

    @staticmethod
    def _csv(rows: list[list[object]]) -> AssetImportFile:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(TEMPLATE_COLUMNS)
        writer.writerows(rows)
        return AssetImportFile("baseline.csv", "text/csv", stream.getvalue().encode())

    @staticmethod
    def _xlsx(rows: list[list[object]]) -> AssetImportFile:
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.append(TEMPLATE_COLUMNS)
        for row in rows:
            sheet.append(row)
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        return AssetImportFile(
            "baseline.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            stream.getvalue(),
        )
