import requests, subprocess, os, shutil, sys
import pacman, utils

# pkg_store holds all packages so that we have all package-objects
# to build the fully interconnected package graph
pkg_store    = {}
srcpkg_store = {}


def parse_dep_pkg(pkgname, ctx, parentpkg=None):
	#packagename = pkgname.split('>=')[0]
	packagename = pkgname.split('=')[0]

	if packagename not in pkg_store:
		pkg_store[packagename] = Package(packagename, firstparent=parentpkg, ctx=ctx)
	elif parentpkg:
		pkg_store[packagename].parents.append(parentpkg)

	return pkg_store[packagename]


def parse_src_pkg(src_id, tarballpath, ctx):
	if src_id not in srcpkg_store:
		srcpkg_store[src_id] = SourcePkg(src_id, tarballpath, ctx=ctx)

	return srcpkg_store[src_id]


def pkg_in_cache(pkg):
	pkgs = []
	pkgprefix = '{}-{}-x86_64.pkg'.format(pkg.name, pkg.version_latest)
	for pkg in os.listdir(pkg.ctx.cachedir):
		if pkgprefix in pkg:
			# was already built at some point
			pkgs.append(pkg)
	return pkgs


class SourcePkg:

	def __init__(self, name, tarballpath, ctx=None):
		self.ctx           = ctx
		self.name          = name
		self.tarballpath   = 'https://aur.archlinux.org' + tarballpath
		self.tarballname   = tarballpath.split('/')[-1]
		self.reviewed      = False
		self.review_passed = False
		self.built         = False
		self.build_success = False
		self.srcdir        = None

	def download(self):
		os.chdir(self.ctx.builddir)
		r = requests.get(self.tarballpath)
		with open(self.tarballname, 'wb') as tarball:
			tarball.write(r.content)

	def extract(self):
		subprocess.call(['tar', '-xzf', self.tarballname])
		os.remove(self.tarballname)
		self.srcdir = os.path.join(self.ctx.builddir, self.name)


	def build(self, buildflags=[]):
		if self.built:
			return self.build_success

		os.chdir(self.srcdir)
		self.built = True

		r = subprocess.call(['makepkg'] + buildflags)
		if r != 0:
			print(":: makepkg for source package {} terminated with exit code {}".format(self.name, r), file=sys.stderr)
			self.build_success = False
			return False
		else:
			self.build_success = True
			return True

	def set_review_state(self, state):
		"""This function is a helper to keep self.review clean and readable"""
		self.review_passed = state
		self.reviewed = True
		return self.review_passed

	def review(self):
		if self.reviewed:
			return self.review_passed

		os.chdir(self.srcdir)

		retval = subprocess.call([os.environ.get('EDITOR') or 'nano', 'PKGBUILD'])
		if 'y' != input('Did PKGBUILD pass review? [y/n] ').lower():
			return self.set_review_state(False)

		if os.path.exists('{}.install'.format(self.name)):
			retval = subprocess.call([os.environ.get('EDITOR') or 'nano', '{}.install'.format(self.name)])
			if 'y' != input('Did {}.install pass review? [y/n] '.format(self.name)).lower():
				return self.set_review_state(False)

		return self.set_review_state(True)

	def cleanup(self):
		shutil.rmtree(self.builddir)


class Package:

	def __init__(self, name, firstparent=None, debug=False, ctx=None):
		self.ctx               = ctx
		self.name              = name
		self.installed         = pacman.is_installed(name)
		self.deps              = []
		self.makedeps          = []
		self.parents           = [firstparent] if firstparent else []
		self.built_pkgs        = []
		self.version_installed = pacman.installed_version(name) if self.installed else None
		self.in_repos          = pacman.in_repos(name)

		self.pkgdata = utils.query_aur("info", self.name, single=True)
		self.in_aur = not self.in_repos and self.pkgdata

		if debug: print('instantate {}; {}; {}'.format(name, "installed" if self.installed else "not installed", "in repos" if self.in_repos else "not in repos"))

		if self.in_aur:
			self.version_latest    = self.pkgdata['Version']

			if "Depends" in self.pkgdata:
				for pkg in self.pkgdata["Depends"]:
					self.deps.append(parse_dep_pkg(pkg, self.ctx))

			if "MakeDepends" in self.pkgdata:
				for pkg in self.pkgdata["MakeDepends"]:
					self.makedeps.append(parse_dep_pkg(pkg, ctx))

			self.srcpkg = parse_src_pkg(self.pkgdata["PackageBase"], self.pkgdata["URLPath"], ctx=ctx)

			self.srcpkg.download()
			self.srcpkg.extract()

	def review(self):
		print("Reviewing", self.name)
		if self.in_repos: 
			return True

		if self.installed and not self.in_aur:
			return True

		if self.srcpkg.reviewed:
			return self.srcpkg.review_passed

		if self.in_aur and len(pkg_in_cache(self)) > 0:
			return True

		for dep in self.deps + self.makedeps:
			if not dep.review():
				return False  # already one dep not passing review is killer, no need to process further

		return self.srcpkg.review()

	def build(self, buildflags=['-Cdf'], recursive=False):
		if self.in_repos or (self.installed and self.version_installed == self.version_latest):
			return True

		pkgs = pkg_in_cache(self)
		if len(pkgs) > 0:
			self.built_pkgs.append(pkgs[0]) # we only need one of them, not all, if multiple ones with different extensions have been built
			return True

		if self.srcpkg.built:
			return self.srcpkg.build_success

		succeeded = self.srcpkg.build(buildflags=buildflags)
		if not succeeded:
			utils.logerr(None, "Build of sources of package {} failed, aborting this subtree".format(self.name))
			return False

		pkgext = os.environ.get('PKGEXT') or 'tar.xz'
		fullpkgname = "{}-{}-x86_64.pkg.{}".format(self.name, self.version_latest, pkgext)
		if fullpkgname in os.listdir(self.srcpkg.srcdir):
			self.built_pkgs.append(fullpkgname)
			shutil.move(os.path.join(self.srcpkg.srcdir, fullpkgname), self.ctx.cachedir)
		else:
			print(" :: Package {} was not found in builddir {}, aborting this subtree".format(fullpkgname, self.srcpkg.srcdir))
			return False

		if recursive:
			for d in self.deps:
				succeeded = d.build(buildflags=buildflags, recursive=True)
				if not succeeded:
					return False  # one dep fails, the entire branch fails immediately, software will not be runnable

		return True

	def get_repodeps(self):
		if self.in_repos:
			return set()  # pacman will take care of repodep-tree
		else:
			rdeps = set()
			for d in self.deps:
				if d.in_repos:
					rdeps.add(d)
				else:
					rdeps.union(d.get_repodeps())
			return rdeps

	def get_makedeps(self):
		if self.in_repos:
			return set()
		else:
			makedeps = set(self.makedeps)
			for d in self.deps:
				makedeps.union(d.get_makedeps())
			return makedeps

	def get_built_pkgs(self):
		pkgs = set(self.built_pkgs)
		for d in self.deps:
			pkgs.union(d.get_built_pkgs())
		return pkgs

	def __str__(self):
		return self.name

	def __repr__(self):
		return str(self)

