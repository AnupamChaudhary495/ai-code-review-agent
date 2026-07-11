import pytest

from review_agent.diffing.classify import detect_language, size_tier
from review_agent.diffing.models import SizeTier


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/app.py", "python"),
        ("web/index.tsx", "typescript"),
        ("Main.java", "java"),
        ("cmd/run.go", "go"),
        ("deploy/main.tf", "terraform"),
        ("Dockerfile", "dockerfile"),
        ("subdir/Makefile", "make"),
        ("docs/README.md", "markdown"),
        ("conf/app.yaml", "yaml"),
        ("data.bin", None),
        ("LICENSE", None),
        ("archive.tar.gz", None),
    ],
)
def test_detect_language(path, language):
    assert detect_language(path) == language


@pytest.mark.parametrize(
    ("changed_lines", "tier"),
    [
        (0, SizeTier.SMALL),
        (50, SizeTier.SMALL),
        (51, SizeTier.MEDIUM),
        (300, SizeTier.MEDIUM),
        (301, SizeTier.LARGE),
        (1500, SizeTier.LARGE),
        (1501, SizeTier.HUGE),
        (100_000, SizeTier.HUGE),
    ],
)
def test_size_tier_boundaries(changed_lines, tier):
    assert size_tier(changed_lines) == tier
