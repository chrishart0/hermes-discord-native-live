from pathlib import Path
import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_public_package_metadata_and_version_agree():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["name"] == "discord-native-live"
    assert manifest["version"] == project["version"]
    assert manifest["homepage"] == project["urls"]["Homepage"]
    assert project["dependencies"]
    assert not any(d.lower().startswith("hermes") for d in project["dependencies"])
    assert "hermes_agent.plugins" not in project.get("entry-points", {})


def test_public_readme_uses_native_installer_and_no_old_branch_install():
    text = (ROOT / "README.md").read_text()
    assert "hermes plugins install chrishart0/hermes-discord-native-live --no-enable" in text
    assert "--branch discord-native-live-plugin" not in text
    assert "allow_gateway_injection: true" in text


def test_live_acceptance_limits_are_disclosed():
    text = (ROOT / "README.md").read_text()
    assert "Experimental" in text and "private instance hooks" in text
    assert "Cloud audio" in text and "restricted parent text channel" in text
