# Contributing

Contributions are welcome, and they are greatly appreciated! Every little bit helps, and credit will always be given.

## Types of Contributions

### Report Bugs

If you are reporting a bug, please include:

* Your operating system name and version, and your Python version.
* Whether you installed chipsynth with the `composer` extra (pygame) or without it.
* The tune, package or command that shows the problem (a few lines of MML is ideal).
* Detailed steps to reproduce the bug.

### Fix Bugs

Look through the GitHub issues for bugs. Anything tagged with "bug" and "help wanted" is open to whoever wants to
implement it.

### Implement Features

Look through the GitHub issues for features. Anything tagged with "enhancement" and "help wanted" is open to whoever
wants to implement it.

### Write Documentation

You can never have enough documentation! Please feel free to contribute to any part of the documentation, such as the
README, the design notes, docstrings, or even on the web in blog posts, articles, and such.

### Submit Feedback

If you are proposing a feature:

* Explain in detail how it would work.
* Keep the scope as narrow as possible, to make it easier to implement.
* Remember that this is a volunteer-driven project, and that contributions are welcome :)

## Get Started!

Ready to contribute? Here's how to set up `chipsynth` for local development.

1. Fork and download a copy of `chipsynth` locally.
2. Install it in editable mode with the composer and the build tools:

    ```console
    $ pip install -e ".[composer,dev]"
    ```

3. Use `git` (or similar) to create a branch for local development and make your changes:

    ```console
    $ git checkout -b name-of-your-bugfix-or-feature
    ```

4. When you're done making changes, run the tests (the dummy drivers let them run without a screen or a sound card):

    ```console
    $ SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m unittest discover -s chipsynth/tests -t .
    ```

5. Commit your changes and open a pull request.

## Commit messages

Releases are made automatically from commit messages ([Conventional Commits](https://www.conventionalcommits.org/)),
so start each commit with a type:

| prefix | releases | use for |
|---|---|---|
| `feat:` | a minor version | a new feature |
| `fix:`, `perf:` | a patch version | a bug fix, a speed-up |
| `feat!:` or a `BREAKING CHANGE:` line in the body | a major version (a minor one while the version is 0.x) | a change that breaks existing use |
| `docs:`, `test:`, `ci:`, `chore:`, `refactor:` | nothing | everything else |

## Pull Request Guidelines

Before you submit a pull request, check that it meets these guidelines:

1. The pull request should include additional tests if appropriate.
2. If the pull request adds functionality, the docs should be updated.
3. The pull request should work for all currently supported operating systems and versions of Python.
4. Code you retire is moved to a `legacy/` folder, not deleted, when it had a purpose.
5. New source files start with the same two header lines as the existing ones (the copyright line and
   `# SPDX-License-Identifier: GPL-3.0-or-later`). By contributing you agree that your contribution is licensed under
   the GPL-3.0-or-later like the rest of the project.

## Code of Conduct

Please note that the `chipsynth` project is released with a Code of Conduct (`CONDUCT.md`). By contributing to this
project you agree to abide by its terms.
