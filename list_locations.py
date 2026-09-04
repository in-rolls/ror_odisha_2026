"""Enumerate the Odisha Bhulekh location tree.

Walks district -> tahsil -> RI circle -> village and writes one row per
village. This is the frame every later stage iterates over, so it runs first
and is checked against the totals the portal publishes about itself.

Checkpointing is per tahsil, not per district. A district can take hours, and
the portal times out often enough that district-level checkpointing loses that
work over and over; a tahsil is minutes, so a timeout costs minutes and the
rerun resumes beside it.

Usage:
    uv run python scripts/data-acquisition/odisha_ror/list_locations.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bhulekh import (  # noqa: E402
    BIND,
    DISTRICT,
    EXPECTED,
    RI,
    SEARCH_TYPE,
    TAHSIL,
    VILLAGE,
    PortalError,
    Session,
)

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw" / "locations"
FRAME = HERE / "raw" / "villages.parquet"

logger = logging.getLogger("locations")


class Walker:
    """Holds a portal session and rebuilds it when the portal times out.

    Attributes:
        pause: Seconds between requests.
    """

    def __init__(self, pause: float) -> None:
        self.pause = pause
        self.session = Session(pause=pause)
        self.session.open()

    def restart(self, attempts: int = 10) -> None:
        """Discard the current session and start a fresh one.

        Transient DNS and connectivity failures are common on this host, and
        a restart that raises would kill a run holding hours of progress, so
        this waits the portal out rather than propagating.

        Args:
            attempts: Times to retry before giving up.
        """
        for attempt in range(1, attempts + 1):
            try:
                self.session = Session(pause=self.pause)
                self.session.open()
                return
            except PortalError as error:
                logger.warning("restart %d/%d: %s", attempt, attempts, error)
                time.sleep(min(60, 5 * attempt))
        raise PortalError(f"could not reopen a session after {attempts} attempts")

    def select_district(self, district: str) -> list[tuple[str, str]]:
        """Select a district and return its tahsils.

        Args:
            district: District code.

        Returns:
            ``(code, name)`` per tahsil.
        """
        self.session.postback(DISTRICT, {DISTRICT: district})
        return self.session.options(TAHSIL)

    def select_tahsil(self, district: str, tahsil: str) -> None:
        """Select a tahsil within the already-selected district.

        Re-posting the district here would reset the RI dropdown to empty,
        so the district is selected once per district by the caller.

        Args:
            district: District code.
            tahsil: Tahsil code.
        """
        self.session.postback(TAHSIL, {DISTRICT: district, TAHSIL: tahsil})


def walk_tahsil(
    walker: Walker,
    district: tuple[str, str],
    tahsil: tuple[str, str],
    count_tenants: bool,
) -> list[dict]:
    """Collect every village under one tahsil.

    Args:
        walker: Session holder.
        district: ``(code, name)`` for the district.
        tahsil: ``(code, name)`` for the tahsil.
        count_tenants: Also record tenants per village, one extra request each.

    Returns:
        One dict per village.
    """
    session = walker.session
    rows: list[dict] = []
    circles = session.options(RI)
    # Some tahsils expose villages directly, with an empty RI dropdown.
    for ri_code, ri_name in circles or [("", "")]:
        scoped = {DISTRICT: district[0], TAHSIL: tahsil[0]}
        if ri_code:
            scoped[RI] = ri_code
            session.postback(RI, scoped)
        found = session.options(VILLAGE)
        logger.info("      %s / %s: %d villages", tahsil[1], ri_name or "-", len(found))
        for village_code, village_name in found:
            row = {
                "district_code": district[0],
                "district_name": district[1],
                "tahsil_code": tahsil[0],
                "tahsil_name": tahsil[1],
                "ri_code": ri_code,
                "ri_name": ri_name,
                "village_code": village_code,
                "village_name": village_name,
                "n_tenant": None,
            }
            if count_tenants:
                fields = dict(scoped)
                fields[VILLAGE] = village_code
                fields[SEARCH_TYPE] = "Tenant"
                session.postback(SEARCH_TYPE, fields)
                row["n_tenant"] = len(session.options(BIND))
            rows.append(row)
    return rows


def collect(walker: Walker, arguments: argparse.Namespace) -> None:
    """Walk every district, writing one JSON file per tahsil.

    Args:
        walker: Session holder.
        arguments: Parsed command line.
    """
    districts = walker.session.options(DISTRICT)
    logger.info("%d districts (expected %d)", len(districts), EXPECTED["districts"])

    for position, district in enumerate(districts[: arguments.districts], start=1):
        folder = RAW / f"district_{district[0]}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            tahsils = walker.select_district(district[0])
        except PortalError as error:
            logger.error("[%d] %s: cannot list tahsils: %s", position, district[1], error)
            walker.restart()
            continue
        logger.info("[%d] %s: %d tahsils", position, district[1], len(tahsils))

        for tahsil in tahsils:
            target = folder / f"tahsil_{tahsil[0]}.json"
            if target.exists():
                continue
            started = time.time()
            rows: list[dict] | None = None
            for attempt in range(1, arguments.retries + 1):
                try:
                    walker.select_tahsil(district[0], tahsil[0])
                    rows = walk_tahsil(walker, district, tahsil, arguments.count_tenants)
                    if rows:
                        break
                    # An empty tahsil means the cascade lost its state, not
                    # that the tahsil has no villages. Checkpointing it would
                    # make a rerun skip it for good.
                    logger.warning("   %s came back empty, retrying", tahsil[1])
                    rows = None
                    walker.restart()
                    walker.select_district(district[0])
                except PortalError as error:
                    logger.warning(
                        "   %s attempt %d/%d: %s",
                        tahsil[1],
                        attempt,
                        arguments.retries,
                        error,
                    )
                    walker.restart()
                    walker.select_district(district[0])
            if not rows:
                logger.error("   %s abandoned after %d attempts", tahsil[1], arguments.retries)
                continue
            target.write_text(json.dumps(rows, ensure_ascii=False))
            logger.info(
                "   %s: %d villages in %.0fs",
                tahsil[1],
                len(rows),
                time.time() - started,
            )


def assemble() -> None:
    """Concatenate every checkpointed tahsil into the village frame."""
    files = sorted(RAW.glob("district_*/tahsil_*.json"))
    if not files:
        raise SystemExit("no tahsils collected")
    villages = pd.concat(
        [pd.DataFrame(json.loads(path.read_text())) for path in files],
        ignore_index=True,
    )
    for column in (
        "district_code",
        "district_name",
        "tahsil_code",
        "tahsil_name",
        "ri_code",
        "ri_name",
        "village_name",
    ):
        villages[column] = villages[column].astype("category")
    villages.to_parquet(FRAME, index=False)

    districts = villages.district_code.nunique()
    tahsils = villages.groupby(["district_code", "tahsil_code"], observed=True).ngroups
    # RI codes repeat across tahsils, so the circle is only unique within
    # (district, tahsil).
    circles = villages.groupby(["district_code", "tahsil_code", "ri_code"], observed=True).ngroups
    print(f"\n{len(villages):,} villages")
    print(f"  districts   {districts:>6} / {EXPECTED['districts']}")
    print(f"  tahsils     {tahsils:>6} / {EXPECTED['tahsils']}")
    print(f"  RI circles  {circles:>6} / {EXPECTED['ri_circles']}")
    print(f"  villages    {len(villages):>6} / {EXPECTED['villages']}")
    print(f"\nwrote {FRAME}")

    duplicates = villages.duplicated(["district_code", "tahsil_code", "village_code"]).sum()
    assert duplicates == 0, f"{duplicates:,} duplicate villages in the frame"

    if districts == EXPECTED["districts"]:
        # Only meaningful on a complete run; a partial pass is expected to
        # fall short and should not fail the script. The portal's counters
        # drift slightly from what it will actually enumerate, so this is a
        # tolerance rather than an equality: it should catch a truncated
        # crawl, not a stale statistic.
        drift = abs(len(villages) - EXPECTED["villages"]) / EXPECTED["villages"]
        assert drift < 0.01, (
            f"{len(villages):,} villages, portal says {EXPECTED['villages']:,} "
            f"({drift:.1%} off)"
        )
        print(
            f"complete: {len(villages):,} villages, "
            f"{drift:+.2%} against the portal's published total, no duplicates"
        )


def main() -> None:
    """Enumerate the location tree and write the village frame."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument(
        "--count-tenants",
        action="store_true",
        help="also record tenants per village (one extra request each)",
    )
    parser.add_argument("--districts", type=int, default=None)
    parser.add_argument(
        "--assemble-only",
        action="store_true",
        help="skip fetching and rebuild the frame from checkpoints",
    )
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    RAW.mkdir(parents=True, exist_ok=True)

    if not arguments.assemble_only:
        collect(Walker(arguments.pause), arguments)
    assemble()


if __name__ == "__main__":
    main()
