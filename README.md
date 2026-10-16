![YC Office Hours](banner.png)

# YC Office Hours

[![skills.sh](https://skills.sh/b/rishiskhare/yc-office-hours)](https://skills.sh/rishiskhare/yc-office-hours)

A coding agent skill that maintains a self-updating index of the entire YC Startup Library, including Paul Graham essays, Startup School, Office Hours, and Lightcone.

## Install

```bash
npx skills add rishiskhare/yc-office-hours -g
```

`-g` installs the skill for every project. Requires Python 3, with no extra packages.

## How it works

The index lives on your machine. When you ask, the agent searches it. Nothing runs until you ask.

The first question builds the index, including older essays and Paul Graham's essays. That takes a few minutes, and it only happens once.

**Self-updating knowledge:** New talks show up on their own. The next time you ask, if the index is older than a day, the agent downloads them before searching. Older essays and Paul Graham's essays refresh about once a month, which can take a few minutes.

The transcripts stay in `~/.cache/yc-office-hours`. This repository does not contain them.

## Update

The library updates by itself. Run this when you want the latest version of the skill from GitHub:

```bash
npx skills update yc-office-hours -g
```

## Uninstall

```bash
npx skills remove yc-office-hours -g
rm -rf ~/.cache/yc-office-hours
```

Remove the skill first. Deleting only the cache is not uninstall: the next question rebuilds it.

## License

[Apache-2.0](LICENSE)
