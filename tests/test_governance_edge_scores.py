import asyncio
import logging
from datetime import date

import pytest

from backend.db.session import Base
from backend.models.analytics import DailyAnalytics
from backend.routes.governance import get_edge_scores
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_edge_scores_handles_string_regime_breakdown():
    """Ensure /api/governance/edge does not crash when regime_breakdown is a string."""

    # Create a transient in-memory DB to mimic the analytics store
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Insert one daily_analytics row where regime_breakdown is a string (bad input)
    today = date.today()
    session = Session()
    session.add(
        DailyAnalytics(
            date=today,
            pnl=0.0,
            total_trades=0,
            regime_breakdown="no data",
        )
    )
    session.commit()

    # Should not raise, and should return a dict response
    result = asyncio.run(get_edge_scores(db=session))
    assert isinstance(result, dict)
    # When there is no real data, the API should still return a baseline structure
    assert "per_regime" in result
    assert "timestamp" in result


def test_edge_scores_logs_warning_when_regime_breakdown_not_dict():
    """When regime_breakdown is not a dict, we should log a warning (and not crash)."""

    from unittest.mock import patch

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    today = date.today()
    session = Session()
    session.add(
        DailyAnalytics(
            date=today,
            pnl=0.0,
            total_trades=0,
            regime_breakdown="no data",
        )
    )
    session.commit()

    from backend.services import log_rate_limit
    log_rate_limit._last_warning_ts = 0.0

    with patch("backend.routes.governance.logger.warning") as mock_warn:
        result = asyncio.run(get_edge_scores(db=session))

    assert isinstance(result, dict)
    assert mock_warn.called
    assert any(
        "regime_breakdown" in args[0] for args, _ in mock_warn.call_args_list
    )

    # Ensure the invalid-count metric increments
    from smc_futures.observability.metrics import GOV_INVALID_REGIME_BREAKDOWN_TOTAL
    assert GOV_INVALID_REGIME_BREAKDOWN_TOTAL._value.get() > 0


def test_edge_scores_handles_none_regime_breakdown_and_warns():
    """When regime_breakdown is None, the endpoint should still work and warn."""

    from unittest.mock import patch

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    today = date.today()
    session = Session()
    session.add(
        DailyAnalytics(
            date=today,
            pnl=0.0,
            total_trades=0,
            regime_breakdown=None,
        )
    )
    session.commit()

    from backend.services import log_rate_limit
    log_rate_limit._last_warning_ts = 0.0

    with patch("backend.routes.governance.logger.warning") as mock_warn:
        result = asyncio.run(get_edge_scores(db=session))

    assert isinstance(result, dict)
    assert mock_warn.called
    assert any(
        "regime_breakdown" in args[0] for args, _ in mock_warn.call_args_list
    )
