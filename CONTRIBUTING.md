Contributing to pretix
======================

Hey there and welcome to pretix!

* We've got a contributors guide in [our documentation](https://docs.pretix.eu/dev/development/contribution/) together with notes on the [development setup](https://docs.pretix.eu/dev/development/setup.html).

* Please note that we have a [Code of Conduct](https://docs.pretix.eu/dev/development/contribution/codeofconduct.html) in place that applies to all project contributions, including issues, pull requests, etc.

* Before we can accept a PR from you we'll need you to sign [our CLA](https://pretix.eu/about/en/cla). You can find more information about the how and why in our [License FAQ](https://docs.pretix.eu/trust/licensing/faq/) and in our [license change blog post](https://pretix.eu/about/en/blog/20210412-license/).

Testing
-------

* **Running tests**: From the project root, run ``pytest src/tests/`` to execute the full suite. To run a subset (e.g. API tests only): ``pytest src/tests/api/``. Use ``-v`` for verbose output and ``-k "test_name"`` to filter by test name.

* **Directory structure**: Tests live under ``src/tests/`` with subdirectories such as ``api/`` (API endpoint tests), ``base/`` (models, services, exporters), ``control/`` (control panel), ``presale/`` (checkout, cart, frontend), and ``plugins/`` (plugin-specific tests). Add new tests in the appropriate layer.

* **Best practices**: Keep tests deterministic—avoid real network calls, use ``freezegun`` for time-sensitive logic instead of ``mock.patch``, and prefer stable selectors for UI tests. Test names should read like specifications for the behavior they protect.

