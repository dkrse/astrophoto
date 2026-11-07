import os
from datetime import datetime, date

from sqlalchemy import (UniqueConstraint, create_engine, String, Text, Integer, BigInteger, Float, Boolean,
                        Date, DateTime, ForeignKey, Table, Column, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

FRAME_TYPES = ["light", "dark", "bias", "flat", "darkflat", "stack", "other"]
CALIB_TYPES = ["dark", "bias", "flat", "darkflat"]
RAW_FRAME_TYPES = ["light", "dark", "bias", "flat", "darkflat"]   # frames used for stacking
FRAME_LABELS = {
    "light": "Light", "dark": "Dark", "bias": "Bias", "flat": "Flat",
    "darkflat": "Dark flat", "stack": "Stack (final)", "other": "Other",
}
# managed lookup lists (stored by name in projects.<kind>)
CATALOG_KINDS = {
    "telescope": "Telescopes", "camera": "Cameras", "mount": "Mounts",
    "software": "Software", "object_type": "Object types", "location": "Locations",
}
CATALOG_SINGULAR = {
    "telescope": "Telescope", "camera": "Camera", "mount": "Mount",
    "software": "Software", "object_type": "Object type", "location": "Location",
}

# technical parameters per equipment kind: (key, label, unit, choices or None) – stored as JSON in CatalogItem.specs
EQUIPMENT_SPECS = {
    "telescope": [("aperture_mm", "Aperture", "mm", None), ("focal_mm", "Focal length", "mm", None),
                  ("telescope_type", "Type", None, ["Refractor", "Newtonian", "SCT", "RC", "Maksutov",
                                                     "Camera lens", "Other"])],
    "camera": [("width_px", "Width", "px", None), ("height_px", "Height", "px", None),
               ("pixel_um", "Pixel size", "µm", None), ("sensor", "Sensor", None, ["Mono", "Color (OSC)", "DSLR"]),
               ("cooled", "Cooling", None, ["Cooled", "Uncooled"])],
    "mount": [("mount_type", "Type", None, ["Equatorial (GEM)", "Harmonic / strain wave", "Alt-Az",
                                            "Star tracker", "Fork"]),
              ("payload_kg", "Payload", "kg", None), ("goto", "GoTo", None, ["Yes", "No"])],
}


class Base(DeclarativeBase):
    pass


project_calibration = Table(
    "project_calibration", Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("calib_set_id", ForeignKey("calibration_sets.id", ondelete="CASCADE"), primary_key=True),
)

# calibration library sets linked to one imaging session (night)
session_calibration = Table(
    "session_calibration", Base.metadata,
    Column("session_id", ForeignKey("imaging_sessions.id", ondelete="CASCADE"), primary_key=True),
    Column("calib_set_id", ForeignKey("calibration_sets.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Project(Base):
    """One object (target). Dates, location and equipment live in its sessions (nights).
    Legacy columns capture_date/location/telescope/camera/mount/software/filters/total_exposure are unused."""
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    target: Mapped[str | None] = mapped_column(String(200))
    object_type: Mapped[str | None] = mapped_column(String(100))
    capture_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(200))
    telescope: Mapped[str | None] = mapped_column(String(200))
    camera: Mapped[str | None] = mapped_column(String(200))
    mount: Mapped[str | None] = mapped_column(String(200))
    filters: Mapped[str | None] = mapped_column(String(200))
    total_exposure: Mapped[str | None] = mapped_column(String(100))
    software: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    cover_file_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    files: Mapped[list["FrameFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="FrameFile.original_name")
    sessions: Mapped[list["ImagingSession"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by=lambda: [ImagingSession.date.asc().nulls_last(), ImagingSession.id])
    calib_sets: Mapped[list["CalibrationSet"]] = relationship(
        secondary=project_calibration, back_populates="projects")


class ImagingSession(Base):
    """One night of imaging within a project."""
    __tablename__ = "imaging_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(200))
    telescope: Mapped[str | None] = mapped_column(String(200))
    camera: Mapped[str | None] = mapped_column(String(200))
    mount: Mapped[str | None] = mapped_column(String(200))
    software: Mapped[str | None] = mapped_column(String(200))
    seeing: Mapped[str | None] = mapped_column(String(50))
    sqm: Mapped[str | None] = mapped_column(String(20))
    temperature: Mapped[str | None] = mapped_column(String(20))
    humidity: Mapped[str | None] = mapped_column(String(20))
    moon_illum: Mapped[float | None] = mapped_column(Float)     # 0-100 %, computed from date
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    project: Mapped[Project] = relationship(back_populates="sessions")
    files: Mapped[list["FrameFile"]] = relationship(back_populates="session")
    calib_sets: Mapped[list["CalibrationSet"]] = relationship(secondary=session_calibration,
                                                              back_populates="sessions")


class CalibrationSet(Base):
    """Shared library of calibration frames (darks, bias, flats …) reusable by projects."""
    __tablename__ = "calibration_sets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    frame_type: Mapped[str] = mapped_column(String(20), index=True)
    capture_date: Mapped[date | None] = mapped_column(Date)
    camera: Mapped[str | None] = mapped_column(String(200))
    telescope: Mapped[str | None] = mapped_column(String(200))
    temperature: Mapped[str | None] = mapped_column(String(20))
    gain: Mapped[str | None] = mapped_column(String(50))
    offset: Mapped[str | None] = mapped_column(String(50))
    exposure_s: Mapped[float | None] = mapped_column(Float)
    filter: Mapped[str | None] = mapped_column(String(50))
    binning: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    files: Mapped[list["FrameFile"]] = relationship(
        back_populates="calib_set", cascade="all, delete-orphan", order_by="FrameFile.original_name")
    projects: Mapped[list[Project]] = relationship(secondary=project_calibration, back_populates="calib_sets")
    sessions: Mapped[list[ImagingSession]] = relationship(secondary=session_calibration, back_populates="calib_sets")


class FrameFile(Base):
    __tablename__ = "frame_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    calib_set_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_sets.id", ondelete="CASCADE"),
                                                     index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("imaging_sessions.id", ondelete="SET NULL"),
                                                   index=True)
    import_job_id: Mapped[int | None] = mapped_column(Integer, index=True)
    frame_type: Mapped[str] = mapped_column(String(20), index=True)
    original_name: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(600))   # relative to DATA_DIR
    thumb_path: Mapped[str | None] = mapped_column(String(600))
    source_path: Mapped[str | None] = mapped_column(Text)   # server import source (while importing)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # importing/pending/done/error
    error: Mapped[str | None] = mapped_column(Text)
    exposure_s: Mapped[float | None] = mapped_column(Float)
    gain: Mapped[str | None] = mapped_column(String(50))
    temperature: Mapped[str | None] = mapped_column(String(50))
    filter: Mapped[str | None] = mapped_column(String(50))
    date_obs: Mapped[datetime | None] = mapped_column(DateTime)
    description: Mapped[str | None] = mapped_column(Text)
    header: Mapped[str | None] = mapped_column(Text)
    view_opts: Mapped[str | None] = mapped_column(Text)
    solve: Mapped[str | None] = mapped_column(Text)         # JSON result of the last ASTAP plate solve
    optics: Mapped[str | None] = mapped_column(Text)        # JSON summary of the last optics analysis
    # frame quality statistics (lights)
    stat_bg: Mapped[float | None] = mapped_column(Float)
    stat_noise: Mapped[float | None] = mapped_column(Float)
    stat_stars: Mapped[int | None] = mapped_column(Integer)
    stat_fwhm: Mapped[float | None] = mapped_column(Float)
    suspect: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)   # last user change of metadata

    project: Mapped[Project | None] = relationship(back_populates="files")
    calib_set: Mapped[CalibrationSet | None] = relationship(back_populates="files")
    session: Mapped[ImagingSession | None] = relationship(back_populates="files")


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    calib_set_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_dir: Mapped[str] = mapped_column(Text)
    skip_dups: Mapped[bool] = mapped_column(Boolean, default=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CatalogItem(Base):
    __tablename__ = "catalog_items"
    __table_args__ = (UniqueConstraint("kind", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    specs: Mapped[str | None] = mapped_column(Text)        # JSON, see EQUIPMENT_SPECS

    @property
    def spec(self) -> dict:
        import json
        try:
            return json.loads(self.specs) if self.specs else {}
        except ValueError:
            return {}


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


# columns added after first release -> applied at startup (idempotent)
MIGRATIONS = [
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS cover_file_id INTEGER",
    "ALTER TABLE frame_files ALTER COLUMN project_id DROP NOT NULL",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS header TEXT",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS view_opts TEXT",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS calib_set_id INTEGER "
    "REFERENCES calibration_sets(id) ON DELETE CASCADE",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS session_id INTEGER "
    "REFERENCES imaging_sessions(id) ON DELETE SET NULL",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS import_job_id INTEGER",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS source_path TEXT",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'pending'",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS date_obs TIMESTAMP",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS stat_bg DOUBLE PRECISION",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS stat_noise DOUBLE PRECISION",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS stat_stars INTEGER",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS stat_fwhm DOUBLE PRECISION",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS suspect BOOLEAN DEFAULT FALSE",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS rejected BOOLEAN DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_frame_files_sha256 ON frame_files (sha256)",
    "CREATE INDEX IF NOT EXISTS ix_frame_files_status ON frame_files (status)",
    "CREATE INDEX IF NOT EXISTS ix_frame_files_calib_set_id ON frame_files (calib_set_id)",
    "CREATE INDEX IF NOT EXISTS ix_frame_files_session_id ON frame_files (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_frame_files_import_job_id ON frame_files (import_job_id)",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
    "ALTER TABLE calibration_sets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now()",
    "ALTER TABLE imaging_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()",
    "ALTER TABLE imaging_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now()",
    "ALTER TABLE catalog_items ADD COLUMN IF NOT EXISTS specs TEXT",
    "ALTER TABLE imaging_sessions ADD COLUMN IF NOT EXISTS software VARCHAR(200)",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS solve TEXT",
    "ALTER TABLE frame_files ADD COLUMN IF NOT EXISTS optics TEXT",
    "UPDATE projects SET created_at = now() WHERE created_at IS NULL",
    "UPDATE projects SET updated_at = created_at WHERE updated_at IS NULL",
]
