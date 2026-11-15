from .storage.database import Database
from .reference.catalog import Catalog
from .reference.calendars import Calendars
from .reference.coverage import Coverage
from .records.intake import Intake
from .records.csv_input import FileIntake
from .records.withdrawals import Withdrawals
from .analysis.engine import AnalysisEngine
from .editions.service import Editions
from .editions.review import Review
from .editions.export import Export
from .editions.investigation import Investigation
from .records.reconciliation import Receipts
from .reference.planning import ReferencePlanning
from .editions.query import EditionQuery
from .editions.readiness import PublicationReadiness


class Ledger:
    def __init__(self, path):
        self.database = Database(path)
        self.catalog = Catalog(self.database)
        self.calendars = Calendars(self.database, self.catalog)
        self.coverage = Coverage(self.database, self.catalog)
        self.intake = Intake(self.database, self.catalog)
        self.files = FileIntake(self.intake)
        self.withdrawals = Withdrawals(self.database, self.intake)
        self.analysis = AnalysisEngine(self.database, self.catalog, self.calendars, self.coverage,
                                       self.intake, self.withdrawals)
        self.editions = Editions(self.database, self.analysis, self.withdrawals)
        self.review = Review(self.database, self.editions, self.withdrawals)
        self.export = Export(self.editions, self.review, self.withdrawals)
        self.investigation = Investigation(self.database, self.editions, self.intake, self.catalog)
        self.receipts = Receipts(self.database, self.intake, self.catalog)
        self.planning = ReferencePlanning(self.database, self.catalog, self.calendars, self.coverage)
        self.query = EditionQuery(self.editions)
        self.publication_readiness = PublicationReadiness(self.database, self.editions, self.review)
        self.withdrawals.editions = self.editions
        self.withdrawals.resume_pending()

    def status(self):
        integrity = self.database.integrity()
        return {"product": "秋窗客流分析账本", "format_version": 1, **integrity}

    def references(self, revision=None):
        with self.database.transaction() as connection:
            revision = self.database.snapshot_revision(connection, revision)
            return {"revision": revision, **self.catalog.snapshot(revision, connection),
                    "calendars": self.calendars.list(revision=revision, connection=connection)}
