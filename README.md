# vogsync

[Français](#français) · [English](#english)

## Français

`vogsync` permet de travailler dans un seul dépôt Git contenant plusieurs
projets 42, puis de déployer chaque projet dans son dépôt Vogsphere séparé.
Ce dépôt contenant tous les projets est appelé **main repo** ; ses répertoires
de projet sont appelés **projects**, sans dépôt Git imbriqué.
La commande `git subtree split` extrait l'historique du projet et place ses
fichiers à la racine du dépôt de destination.

### Prérequis

- Linux ou macOS, Python 3.9 ou plus récent, Git et `git subtree`.
- Un compte GitHub et l'accès aux URL Vogsphere fournies par votre école.
- Une connexion et les droits d'accès aux dépôts concernés. Vogsphere peut
  n'être accessible que depuis le campus.

Aucun paquet Python ni droit administrateur n'est nécessaire.

### Installation sous Linux

Depuis le répertoire contenant `install.sh` et `vogsync.py` :

```sh
./install.sh
```

L'exécutable est copié dans `~/.local/bin`. L'installateur refuse d'écraser un
fichier existant. Si ce répertoire n'est pas dans votre `PATH`, ajoutez cette
ligne à la configuration de votre shell, puis rechargez-la :

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Sur macOS, vous pouvez aussi lancer `python3 /chemin/vers/vogsync.py`.

### Mise à jour

Depuis votre clone de ce dépôt :

```sh
git pull --ff-only
./update.sh
```

`update.sh` remplace atomiquement `~/.local/bin/vogsync` par le `vogsync.py`
de ce clone, après vérification de sa syntaxe Python. Il nécessite une
installation existante et refuse les liens symboliques. Il ne lance pas de
pull automatiquement et ne modifie pas la configuration de vos projets.

### Configuration pas à pas

#### 1. Créer le dépôt GitHub

1. Créez un compte sur [GitHub](https://github.com/) si nécessaire.
2. Créez un dépôt **privé** nommé `42-cursus`, vide ou initialisé avec un README.
3. Configurez votre authentification GitHub, par exemple avec une
   [clé SSH](https://docs.github.com/fr/authentication/connecting-to-github-with-ssh).
4. Copiez l'URL SSH ou HTTPS de clonage du dépôt.

#### 2. Cloner le dépôt et organiser les projets

```sh
git clone git@github.com:VOTRE_PSEUDO/42-cursus.git
cd 42-cursus
git config user.name 'Votre nom'
git config user.email 'votre-email@example.com'
mkdir -p libft ft_printf
# Ajoutez ensuite les fichiers de chaque projet dans son répertoire.
```

Remplacez l'URL et l'identité par les vôtres. Utilisez **un seul dépôt Git**,
avec un répertoire par projet directement à sa racine ; ne créez pas de dépôt
Git imbriqué dans chaque projet :

```text
42-cursus/                # Main repo relié à GitHub
├── .git/
├── .gitignore
├── .vogsync.json         # URL Vogsphere locales, ignorées par Git
├── libft/
│   ├── Makefile
│   └── srcs/
└── ft_printf/
    ├── Makefile
    └── srcs/
```

Git ne suit pas les répertoires vides : ajoutez des fichiers avant de les
committer. Lors du déploiement de `libft`, son `Makefile` et son répertoire
`srcs/` seront à la racine du dépôt Vogsphere, sans le dossier parent `libft/`.

#### 3. Créer le premier commit et le pousser sur GitHub

Après avoir ajouté vos fichiers :

```sh
git add .
git commit -m 'Add initial projects'
git branch -M main
git push -u origin main
```

`-u` configure le suivi de la branche distante utilisé par les options de push
et de pull de `vogsync`. Si votre dépôt a déjà une branche et un suivi configuré,
conservez-les plutôt que de renommer la branche.

#### 4. Associer chaque projet à son dépôt Vogsphere

Récupérez auprès de votre école l'URL Vogsphere propre à chaque projet :

```sh
vogsync remote set libft 'URL_VOGSPHERE_LIBFT'
vogsync remote set ft_printf 'URL_VOGSPHERE_FT_PRINTF'
vogsync remote list

# Vérifiez puis commitez la règle .gitignore ajoutée si nécessaire.
git diff -- .gitignore
git add .gitignore
git commit -m 'Ignore local Vogsphere configuration'
git push
```

Les URL sont stockées localement dans `.vogsync.json`, ignoré par Git ; ne
committez pas ce fichier. Reconfigurez les URL sur chaque ordinateur. Si le
fichier est déjà suivi par Git, une règle `.gitignore` ne suffit pas à le
retirer de l'index : examinez également son historique.

#### 5. Travailler chez soi et déployer au campus

Chez vous, commitez et poussez vos changements vers le main repo GitHub :

```sh
cd libft
git add .
vogsync commit -m 'Implement list functions' --push
```

Sur un ordinateur du campus, clonez le main repo, installez `vogsync`, puis :

```sh
cd 42-cursus
git pull
vogsync remote set libft 'URL_VOGSPHERE_LIBFT'
vogsync render libft --dry-run
vogsync render libft
```

Le répertoire de travail doit être propre pour `render` : commitez ou mettez
de côté les changements avant de déployer. Seul l'historique déjà commité du
projet est extrait. Le dry-run vérifie la connexion en lecture, pas les droits
de push ; il ne pousse aucune référence.

**Attention :** `render` effectue un push forcé sur la branche distante choisie
(`master` par défaut). Il peut écraser son historique. Vérifiez toujours l'URL
et la branche de destination avant de déployer.

### Commandes utiles

```sh
vogsync remote get libft             # Afficher l'URL Vogsphere
vogsync remote list                  # Lister les URL configurées
vogsync commit -m 'Fix a bug'        # Commit préfixé selon le dossier courant
vogsync commit libft -a -m 'Fix'     # Modifications suivies de libft uniquement
vogsync commit -m 'Fix' --push       # Puis pousser vers le suivi Git
vogsync render libft --branch main   # Déployer sur une autre branche distante
vogsync render libft --pull           # Mettre à jour le main repo avant le split
vogsync status                       # État local, sans accès réseau
vogsync status --remote              # Comparer chaque projet à master distant
vogsync doctor                       # Diagnostiquer Git et l'architecture locale
vogsync doctor --remote              # Vérifier aussi l'accès aux dépôts distants
vogsync --help
```

Sans `-a`, `commit` inclut **tout l'index**, y compris les changements préparés
hors du projet. Avec `-a` et un projet actif ou indiqué, le commit est limité à
ce projet et les autres changements indexés restent dans l'index ; les nouveaux
fichiers non suivis ne sont pas ajoutés automatiquement. À la racine sans nom de
projet, le commit n'a pas de préfixe et `-a` concerne tout le dépôt.

`--push` et `--pull` nécessitent une branche avec un suivi distant configuré.
`--pull` utilise `git pull --ff-only` et peut modifier la branche locale même
avec `--dry-run`. `status --remote` vérifie les dépôts séquentiellement, avec
un délai SSH de 2 secondes et une limite totale de 10 secondes par interrogation.
Il compare toujours à `master`, même si vous déployez sur une autre branche.

### Diagnostiquer la configuration

```sh
vogsync doctor
vogsync doctor --remote --branch master
```

`doctor` affiche des résultats `OK`, `WARN` et `ERROR`, puis un bilan. Il vérifie
Git et `git subtree`, l'intégrité des objets Git, le premier commit, l'identité,
la branche et son suivi, les changements locaux et les opérations Git en cours.
Il recherche les dépôts imbriqués et les sous-modules, vérifie les répertoires et
les fichiers commités des projets, ainsi que la validité, les permissions et
l'exclusion Git de `.vogsync.json`. Il signale aussi les destinations partagées
entre projets ou correspondant au main repo, dangereuses avec le push forcé.
Les répertoires commités sans URL produisent un avertissement ; cela peut être
normal pour de la documentation ou des outils.

L'identité Git affiche les valeurs effectives de `user.name` et `user.email`.
Tous les projets sont vérifiés, même sans URL de déploiement. Les nouveaux
répertoires non ignorés sont également détectés ; ils doivent être commités.
Pour exclure un répertoire qui n'est pas un projet, éditez `.vogsync.json` :

```json
{
  "exclude": ["other"],
  "libft": "URL_VOGSPHERE_LIBFT",
  "ft_printf": "URL_VOGSPHERE_FT_PRINTF"
}
```

`exclude` est une clé réservée contenant des noms exacts de répertoires à la
racine (pas de chemins ni de motifs). Les anciennes configurations restent
valides et `remote set` préserve cette liste. Un répertoire exclu ne peut pas
avoir d'URL de projet. L'exclusion retire les vérifications propres au projet,
mais pas les contrôles de sécurité du main repo (dépôts imbriqués, index,
conflits, intégrité). Elle ne modifie pas les règles Git ni les fichiers suivis.
L'avertissement « No project Vogsphere URLs configured » concerne les URL
Vogsphere des projets, pas le remote `origin` du main repo.

Le diagnostic ne modifie aucun fichier ni aucune référence et ne fait ni split,
ni fetch, ni push. Par défaut, il est hors ligne ; les comparaisons au suivi
utilisent les références locales, potentiellement anciennes. `--remote` vérifie
l'accès en lecture au remote du main repo et aux dépôts des projets (10 secondes
maximum par interrogation), pas les droits de push ni la synchronisation.
`--branch` choisit la branche des projets (`master` par défaut). Le code de sortie
est `1` si une erreur est détectée, `0` sinon, même avec des avertissements.

### GitHub, origin et Vogsphere

Le remote Git du main repo et les URL Vogsphere sont distincts. `vogsync`
interroge Git pour connaître le suivi de la branche courante ; il ne parcourt
pas directement `.git`. Si ce suivi utilise `origin`, une modification avec
`git remote set-url origin NOUVELLE_URL` sera prise en compte pour les prochains
push et pull. Si le suivi utilise un autre remote, modifier `origin` ne le
change pas.

En revanche, `render` utilise l'URL du projet dans `.vogsync.json` : modifiez-la
avec `vogsync remote set PROJET NOUVELLE_URL_VOGSPHERE`. Ne pointez pas cette
URL vers la branche de votre main repo GitHub : le déploiement est forcé.

### Comprendre les notions Git

- **Dépôt et commit :** le dépôt conserve les fichiers et leur historique ; un
  commit enregistre une version. [Les bases de Git](https://git-scm.com/book/fr/v2/Les-bases-de-Git-Enregistrer-des-modifications-dans-le-d%C3%A9p%C3%B4t).
- **Index / staging :** `git add` prépare des changements pour le prochain
  commit. [Enregistrer des modifications](https://git-scm.com/book/fr/v2/Les-bases-de-Git-Enregistrer-des-modifications-dans-le-d%C3%A9p%C3%B4t).
- **Branche et `main` :** une branche est un pointeur vers un commit ; `main`
  est un nom courant pour la branche principale, pas une obligation.
  [Les branches Git](https://git-scm.com/book/fr/v2/Les-branches-avec-Git-Les-branches-en-bref).
- **`HEAD` et detached HEAD :** `HEAD` indique normalement la branche courante ;
  en mode détaché, il pointe directement vers un commit. Les opérations qui
  nécessitent le suivi d'une branche ne fonctionnent alors pas.
  [Documentation de git switch](https://git-scm.com/docs/git-switch#_detached_head).
- **Remote, `origin` et upstream :** un remote nomme un dépôt distant ; `origin`
  est le nom habituel du dépôt cloné. L'upstream est la branche distante suivie
  par une branche locale. [Dépôts distants](https://git-scm.com/book/fr/v2/Les-bases-de-Git-Travailler-avec-des-d%C3%A9p%C3%B4ts-distants) ·
  [Branches de suivi](https://git-scm.com/book/fr/v2/Les-branches-avec-Git-Les-branches-distantes).
- **Push forcé :** `--force` peut remplacer l'historique distant.
  [Documentation de git push](https://git-scm.com/docs/git-push#Documentation/git-push.txt---force).

---

## English

`vogsync` manages project-specific Vogsphere deployments from a Git monorepo.
It uses `git subtree split` to extract a project's committed history and put its
files at the root of the destination repository. The monorepo stays unchanged
by the deployment, apart from the Git objects created by the split.

## Requirements

- Linux or macOS, with Python 3.9 or newer and Git, including `git subtree`.
- A Git monorepo with projects as top-level directories.
- Network access and the appropriate credentials for your Git remotes. Vogsphere
  may only be reachable from campus.

No Python packages or root privileges are required.

## Installation on Linux

From the directory containing `install.sh` and `vogsync.py`, run:

```sh
./install.sh
```

This copies the executable to `~/.local/bin/vogsync`.

The installer will not overwrite an existing file. If `~/.local/bin` is not in
your `PATH`, add the following to your shell configuration, then start a new
shell or reload that configuration:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

On macOS, you can also run `python3 /path/to/vogsync.py` directly.

## Set up your monorepo from scratch

### 1. Create your GitHub account and repository

1. Create an account at [github.com](https://github.com/) if you do not have one.
2. Create a **private** repository named `42-cursus`. You can initialize it with
   a README, or start with an empty repository.
3. Set up GitHub authentication on your computer, for example by adding an SSH
   key to your GitHub account. See [GitHub's SSH documentation](https://docs.github.com/en/authentication/connecting-to-github-with-ssh).
4. Copy the repository's SSH or HTTPS clone URL from GitHub.

### 2. Clone it and create project directories

```sh
git clone git@github.com:YOUR_USERNAME/42-cursus.git
cd 42-cursus
git config user.name 'Your Name'
git config user.email 'your-email@example.com'

mkdir -p libft ft_printf
# Add your project files inside these directories.
```

Replace the clone URL and Git identity with your own. Git does not track empty
directories, so add actual project files before committing them.

The expected layout is one Git repository with a **top-level directory per
project**, not a separate nested Git repository for each project:

```text
42-cursus/                 # One monorepo, connected to GitHub as origin
├── .git/                  # Git's local repository data
├── .gitignore
├── .vogsync.json          # Local, ignored Vogsphere URL mappings
├── libft/
│   ├── Makefile
│   └── srcs/
└── ft_printf/
    ├── Makefile
    └── srcs/
```

When you deploy `libft`, its `Makefile` and `srcs/` appear at the root of the
separate libft Vogsphere repository. Only committed history affecting that
directory is included. Files outside `libft/` are not deployed.

### 3. Commit and connect the monorepo branch

After adding project files, create an initial commit and push it to GitHub:

```sh
git add .
git commit -m 'Add initial projects'
git branch -M main
git push -u origin main
```

The `-u` flag configures the upstream used by `vogsync commit --push` and
`vogsync render --pull`. If you already have an established branch and upstream,
use those instead of renaming your branch.

### 4. Configure a Vogsphere destination for each project

Obtain the dedicated Vogsphere URL for each project from your school. From the
monorepo, associate each existing project directory with its own URL:

```sh
vogsync remote set libft 'YOUR_LIBFT_VOGSPHERE_URL'
vogsync remote set ft_printf 'YOUR_FT_PRINTF_VOGSPHERE_URL'
vogsync remote list

# Review the ignore rule added by the first remote set, then commit it.
git diff -- .gitignore
git add .gitignore
git commit -m 'Ignore local Vogsphere configuration'
git push
```

`.vogsync.json` is intentionally local and ignored, so repeat the `remote set`
commands on each computer where you want to deploy. Do not commit that file.

### 5. Work at home and deploy from campus

At home, commit and push changes to your GitHub monorepo:

```sh
cd libft
git add .
vogsync commit -m 'Implement list functions' --push
```

On a campus computer, clone the same GitHub monorepo, install `vogsync`, and
configure the project URL locally as in step 4. After pulling your monorepo
changes, check and deploy the project:

```sh
git pull
vogsync remote set libft 'YOUR_LIBFT_VOGSPHERE_URL'
vogsync render libft --dry-run
vogsync render libft
```

### GitHub origin versus Vogsphere URLs

These are separate destinations:

- **GitHub monorepo remote:** Git stores `origin` in its repository
  configuration. `vogsync commit --push` and `vogsync render --pull` use the
  current branch's configured upstream, which is often `origin`. They ask Git
  for that configuration; they do not read `.git` files directly. If that
  upstream uses `origin`, changing its URL with `git remote set-url origin URL`
  is picked up by subsequent pushes and pulls. If the branch uses a different
  upstream remote, changing `origin` does not change that upstream.
- **Vogsphere project URLs:** `vogsync render` reads the project's URL from
  `.vogsync.json`, not from `origin`. Change it with
  `vogsync remote set PROJECT NEW_VOGSPHERE_URL`.

This separation is important: `render` force-pushes a project's extracted
history and must not be pointed at your GitHub monorepo branch.

## Example workflow

Suppose your monorepo contains `libft/` and `ft_printf/`:

```sh
cd ~/42-cursus

# Store the project's Vogsphere URL locally.
vogsync remote set libft 'git@vogsphere.example:libft'
vogsync remote list

# The first remote set may update .gitignore. Review and commit it.
git diff -- .gitignore
git add .gitignore
git commit -m 'Ignore local Vogsphere configuration'

# Work on the project and create a prefixed monorepo commit.
cd libft
git add .
vogsync commit -m 'Implement string functions'

# On campus, check connectivity before deploying.
vogsync render libft --dry-run
vogsync render libft
```

Replace the example URL with the repository URL supplied by your school.
`render` deploys committed history only and requires a clean working tree.

## Commands

### Manage remotes

```sh
vogsync remote set libft 'git@vogsphere.example:libft'
vogsync remote get libft
vogsync remote list
```

Mappings are stored as a JSON object in `.vogsync.json` at the repository root.
The first `remote set` adds an ignore rule if needed. The file is created with
permissions `0600`; existing file permissions are not changed. If the file was
already tracked by Git, an ignore rule alone will not untrack it: inspect its
history and remove it from the index as appropriate.

### Commit changes

```sh
# Inside libft/, creates a commit named "[libft] Fix memory leak".
vogsync commit -m 'Fix memory leak'

# Include tracked modifications and deletions in libft/.
vogsync commit -a -m 'Clean up error handling'

# From the repository root, specify the project explicitly.
vogsync commit libft -a -m 'Fix edge cases'

# Push the commit to the current branch's configured upstream.
vogsync commit -m 'Finish tests' --push
```

The active project is the top-level directory containing the current working
directory. At the repository root, omitting the project creates an unprefixed
commit with a warning. `-a` includes tracked modifications and deletions, not
new untracked files. With an active or specified project, `-a` limits the commit
to that project; changes staged in other projects remain staged. Without `-a`,
the commit includes **all staged changes**, even those outside the project. At
the repository root with no project, `-a` applies to the whole repository.

`--push` requires a branch with a configured upstream; it is not available from
a detached HEAD. A failed push does not undo the commit.

### Deploy a project

```sh
vogsync render libft --dry-run       # Split and check remote connectivity
vogsync render libft                 # Deploy to remote master
vogsync render libft --branch main   # Deploy to a different remote branch
vogsync render libft --pull          # Fast-forward from the upstream first
```

The dry run performs the split and checks remote read connectivity, but does not
verify push permissions or push any refs. `--pull` requires a configured
upstream and uses `git pull --ff-only`; it can update the local branch even
when combined with `--dry-run`.

**Warning:** deployment force-pushes to the selected remote branch and can
overwrite its existing history. Check that the destination is correct before
running it. The split is pushed directly by commit ID; no temporary local branch
is created.

### Check status

```sh
vogsync status            # Local repository status and configured remotes
vogsync status --remote   # Also compare each project against remote master
```

The default status does not contact the network. With `--remote`, each remote
check uses a two-second SSH connection timeout and a ten-second overall
process timeout. Checks are sequential, so multiple unreachable remotes may
still take time. Synchronization is compared against `master`, even if you
deploy to another branch with `render --branch`.

### Help and version

```sh
vogsync --help
vogsync render --help
vogsync --version
```

Commands return `0` on success, `1` for operational errors and `2` for invalid
command-line arguments.

## Git concepts and further reading

- **Repository and commit:** A repository holds your files and history; a
  commit records a version. [Recording changes](https://git-scm.com/book/en/v2/Git-Basics-Recording-Changes-to-the-Repository).
- **Index / staging area:** `git add` prepares changes for a commit.
  [Recording changes](https://git-scm.com/book/en/v2/Git-Basics-Recording-Changes-to-the-Repository).
- **Branch and `main`:** A branch points to a commit; `main` is a common name
  for the primary branch, not a requirement.
  [Branches in a nutshell](https://git-scm.com/book/en/v2/Git-Branching-Branches-in-a-Nutshell).
- **`HEAD` and detached HEAD:** `HEAD` normally identifies the current branch;
  when detached, it points directly to a commit instead. Operations requiring a
  branch upstream will not work in that state.
  [Detached HEAD documentation](https://git-scm.com/docs/git-switch#_detached_head).
- **Remote, `origin`, and upstream:** A remote names another repository;
  `origin` is the usual name for the repository you cloned. An upstream is the
  remote branch tracked by a local branch.
  [Working with remotes](https://git-scm.com/book/en/v2/Git-Basics-Working-with-Remotes) ·
  [Tracking branches](https://git-scm.com/book/en/v2/Git-Branching-Remote-Branches).
- **Force push:** `--force` can replace remote history.
  [git push documentation](https://git-scm.com/docs/git-push#Documentation/git-push.txt---force).
