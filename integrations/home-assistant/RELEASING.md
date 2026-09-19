# Splitting and releasing the Home Assistant integration

`pyopennvr/` and `hass-opennvr/` are each laid out as the root of the repository they become, so splitting them off needs no restructuring. Their `.github/workflows` do nothing inside this monorepo; they start running in the new repositories. Until the split, the monorepo's own CI job `home-assistant` runs the same tests.

End users can't install the integration until `pyopennvr` is on PyPI, because Home Assistant installs an integration's requirements from PyPI.

## Decisions needed first (owner)

1. **Who publishes:** which GitHub organisation owns the two repositories, and which PyPI account owns `pyopennvr`.
2. **Licences:** `pyopennvr` is Apache-2.0, to match the app SDK, and ships that `LICENSE`. `hass-opennvr` has no licence yet; HACS and Home Assistant core expect one (Apache-2.0 would match).
3. **Code owners:** the GitHub handles for `codeowners` in `hass-opennvr/custom_components/opennvr/manifest.json`. The quality scale's `integration-owner` rule waits on this.

## 1. Create the repositories

On GitHub, create empty repositories `open-nvr/pyopennvr` and `open-nvr/hass-opennvr`, with no README or licence, so the first push is the history.

## 2. Split and push

From the monorepo root, on the branch that holds the release:

```sh
git subtree split --prefix=integrations/home-assistant/pyopennvr -b split/pyopennvr
git push git@github.com:open-nvr/pyopennvr.git split/pyopennvr:main

git subtree split --prefix=integrations/home-assistant/hass-opennvr -b split/hass-opennvr
git push git@github.com:open-nvr/hass-opennvr.git split/hass-opennvr:main
```

Later releases repeat the same split. It is incremental, and pushes only the new commits.

## 3. Publish pyopennvr

1. On PyPI, add a **trusted publisher** for the project `pyopennvr`: repository `open-nvr/pyopennvr`, workflow `publish.yml`, environment `pypi`. No token is stored anywhere.
2. In `open-nvr/pyopennvr`, create the environment `pypi` (Settings > Environments).
3. Set `[project.urls] Homepage` in `pyproject.toml` to the new repository.
4. Tag and publish a GitHub release `v0.1.0`. The `Publish` workflow builds and uploads it.

Checked locally: `python -m build` and `twine check dist/*` pass. The wheel carries the package, `py.typed` and the licence.

## 4. Release the integration

1. Check that `requirements` in `manifest.json` names the published version (`pyopennvr==0.1.0`), and raise `version` for later releases.
2. Add the licence (decision 2).
3. In `open-nvr/hass-opennvr`, the `Validate` workflow runs hassfest, the HACS action and the tests. They install `pyopennvr` from PyPI, so they pass once step 3 is done.
4. Publish a GitHub release. HACS offers releases to users who add the repository as a custom repository; being listed in HACS by default needs a submission to the HACS default list.

## 5. Keep the monorepo in step

The server contract (`server/contract/`) stays in the monorepo. The fixtures under each `tests/fixtures` are copies of it, checked for drift while they live here. After the split, update them from the server's copy when the contract changes, and bump the integration's supported contract when the major version changes.
