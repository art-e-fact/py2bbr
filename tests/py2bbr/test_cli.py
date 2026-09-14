"""
Unit tests on CLI functions
"""

import pytest

from py2bbr import cli
from py2bbr.errors import Py2BbrError


class TestParsePackageSpec:
    def test_name_no_spec(self):
        assert cli._parse_package_spec("name") == ("name", None)

    def test_name_exact_version(self):
        assert cli._parse_package_spec("name==1.2.3") == ("name", "1.2.3")
        assert cli._parse_package_spec("name===1.2.3") == ("name", "1.2.3")

    def test_name_fuzzy_version(self):
        assert cli._parse_package_spec("name>1.2.3") == ("name", None)

    def test_name_fuzzy_error(self):
        with pytest.raises(
            Py2BbrError, match="invalid package spec 'bad name'"
        ):
            cli._parse_package_spec("bad name")
