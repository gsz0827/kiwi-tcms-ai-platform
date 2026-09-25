default: help

VERSION = $(shell python -m tcms)
FLAKE8_EXCLUDE=.git
RUNTIME_BASE := $(or $(RUNTIME_BASE), "quay.io/centos/centos:stream10")

.PHONY: flake8
flake8:
	@flake8 --exclude=$(FLAKE8_EXCLUDE) tcms *.py kiwi_lint tcms_settings_dir


DJANGO_SETTINGS_MODULE="tcms.settings.test"

ifeq ($(TEST_DB),MySQL)
	DJANGO_SETTINGS_MODULE="tcms.settings.test.mariadb"
endif

ifeq ($(TEST_DB),MariaDB)
	DJANGO_SETTINGS_MODULE="tcms.settings.test.mariadb"
endif

ifeq ($(TEST_DB),Postgres)
	DJANGO_SETTINGS_MODULE="tcms.settings.test.postgresql"
endif


.PHONY: test
test:
	./manage.py compilemessages
	if [ "$$TEST_DB" == "all" ]; then \
		for DB in SQLite MySQL Postgres MariaDB; do \
			TEST_DB=$$DB make test; \
		done; \
	else \
		PYTHONWARNINGS=d coverage run --source='.' ./manage.py test -v2 --noinput --settings=$(DJANGO_SETTINGS_MODULE); \
	fi


# test for missing migrations
# https://stackoverflow.com/questions/54177838/
.PHONY: test_for_missing_migrations
test_for_missing_migrations:
	./manage.py migrate --settings=$(DJANGO_SETTINGS_MODULE)
	./manage.py makemigrations --check --settings=$(DJANGO_SETTINGS_MODULE)

.PHONY: check
check: flake8 test


# ---------------------------------------------------------------------------
# AI 平台（docker-compose.ai.yml）
#
# 重要：tests / tests-mariadb 两个服务在 compose 里固定引用 kiwi-tcms-ai:test
# 镜像，而 `docker compose run` 默认不会重建镜像。直接 run 会让测试跑在旧镜像
# 的代码上，出现"改了代码但测试结果不变"的假象。下面的目标一律先 build 再 run。
# ---------------------------------------------------------------------------
AI_COMPOSE := docker compose -f docker-compose.ai.yml

# 探针地址。改过 .env 里的 KIWI_HTTPS_PORT 时用 make ai-health AI_BASE_URL=... 覆盖
AI_BASE_URL ?= https://localhost:9443

.PHONY: ai-up
ai-up:
	$(AI_COMPOSE) up --build -d

.PHONY: ai-down
ai-down:
	$(AI_COMPOSE) down

.PHONY: ai-logs
ai-logs:
	$(AI_COMPOSE) logs -f --tail=100 web worker scheduler

.PHONY: ai-migrate
ai-migrate:
	$(AI_COMPOSE) exec web python manage.py migrate

.PHONY: ai-test-image
ai-test-image:
	$(AI_COMPOSE) --profile test-mariadb build tests-mariadb

.PHONY: ai-test
ai-test: ai-test-image
	$(AI_COMPOSE) --profile test-mariadb run --rm -T tests-mariadb

# 校验模型改动是否都有对应迁移，避免部署时才暴露缺迁移
.PHONY: ai-test-missing-migrations
ai-test-missing-migrations: ai-test-image
	$(AI_COMPOSE) --profile test-mariadb run --rm -T tests-mariadb \
	    python manage.py makemigrations --check --dry-run \
	    --settings=tcms.settings.ai_test_mariadb


# 探针自检：health 返回 200 说明进程可用（不依赖数据库），
# ready 返回 200 说明数据库连通且迁移已跑完；非 200 会打印原因。
.PHONY: ai-health
ai-health:
	@printf 'health  -> '; curl -k -s -o /dev/null -w '%{http_code}\n' $(AI_BASE_URL)/health/
	@printf 'ready   -> '; curl -k -s -o /dev/null -w '%{http_code}\n' $(AI_BASE_URL)/ready/
	@curl -k -s $(AI_BASE_URL)/ready/ | python3 -m json.tool || true


.PHONY: pylint
pylint:
	pylint --load-plugins=pylint.extensions.no_self_use -d missing-docstring *.py kiwi_lint/

	PYTHONPATH=.:./tcms/ DJANGO_SETTINGS_MODULE=$(DJANGO_SETTINGS_MODULE) \
	    pylint                                                            \
	        --load-plugins=pylint_django.checkers.migrations              \
	        --load-plugins=pylint.extensions.no_self_use                  \
	    -d missing-docstring -d duplicate-code -d new-db-field-with-default --module-naming-style=any  tcms/*/migrations/*

	PYTHONPATH=.:./tcms/ DJANGO_SETTINGS_MODULE=$(DJANGO_SETTINGS_MODULE) \
	    pylint                                                            \
	        --load-plugins=pylint_django                                  \
	        --load-plugins=kiwi_lint                                      \
	        --load-plugins=pylint.extensions.docparams                    \
	        --load-plugins=pylint.extensions.no_self_use                  \
	    -d missing-docstring -d duplicate-code -d one-to-one-field -d similar-string \
	    --ignore migrations tcms/ tcms_settings_dir/

.PHONY: similar_strings
similar_strings:
	PYTHONPATH=.:./tcms/ DJANGO_SETTINGS_MODULE=$(DJANGO_SETTINGS_MODULE) pylint --load-plugins=kiwi_lint --load-plugins=pylint_django --load-plugins=pylint.extensions.no_self_use -d all -e similar-string tcms/ tcms_settings_dir/

.PHONY: bandit
bandit:
	bandit -r *.py tcms/ kiwi_lint/ tcms_settings_dir/

.PHONY: build-pkg
build-pkg:
	rm -rf dist/
	docker build --build-arg RUNTIME_BASE=$(RUNTIME_BASE) --output type=local,dest=dist/ --target pkg-dist .

.PHONY: upload-pkg
upload-pkg: build-pkg
	test -n "$(TWINE_PASSWORD)" || exit 2
	curl -F p1=@dist/kiwitcms-$(VERSION).tar.gz -F p1_language=python https://$(TWINE_PASSWORD)@push.fury.io/kiwitcms/
	curl -F p1=@dist/kiwitcms-$(VERSION)-py3-none-any.whl -F p1_language=python https://$(TWINE_PASSWORD)@push.fury.io/kiwitcms/

.PHONY: docker-image
docker-image:
	docker build --no-cache \
	    --build-arg RUNTIME_BASE=$(RUNTIME_BASE) \
	    -t pub.kiwitcms.eu/kiwitcms/kiwi:latest .


.PHONY: docker-manifest
docker-manifest:
	docker manifest create \
	    quay.io/kiwitcms/upstream:$(VERSION) \
	    quay.io/kiwitcms/upstream:$(VERSION)-x86_64 \
	    quay.io/kiwitcms/upstream:$(VERSION)-aarch64
	docker manifest push quay.io/kiwitcms/upstream:$(VERSION)


.PHONY: test-docker-image
test-docker-image: docker-image
	./tests/runner.sh

.PHONY: docs
docs:
	make -C docs/ html

# checks if all of our documentation/source files are under git!
# this is necessary because ReadTheDocs doesn't call `make' but uses
# conf.py and builds the documentation itself! Since we have some
# auto-generated API docs we want to make sure that we didn't forget
# to regenerate them after code changes!
.PHONY: check-docs-source-in-git
check-docs-source-in-git: docs
	git status
	if [ -n "$$(git status --short)" ]; then \
	    git diff; \
	    echo "FAIL: unmerged docs changes. Pobably auto-generated!"; \
	    echo "HELP: execute 'make docs' and commit to fix this"; \
	    exit 1; \
	fi

.PHONY: doc8
doc8:
	doc8 docs/source *.rst

.PHONY: help
help:
	@echo 'Usage: make [command]'
	@echo ''
	@echo 'Available commands:'
	@echo ''
	@echo '  flake8           - Check Python code style throughout whole source code tree'
	@echo '  check            - Run all tests.'
	@echo '  ai-up            - Build and start the AI platform (web, worker, scheduler, db)'
	@echo '  ai-down          - Stop the AI platform'
	@echo '  ai-logs          - Follow logs for web, worker and scheduler'
	@echo '  ai-migrate       - Apply database migrations inside the running web container'
	@echo '  ai-test          - Rebuild the test image, then run the AI test suite'
	@echo '  ai-test-missing-migrations - Fail if model changes lack migrations'
	@echo '  ai-health        - Probe /health/ and /ready/ on the running platform'
	@echo '  docker-image     - Build Docker image'
	@echo '  docker-manifest  - Build Docker manifest for multi-arch images'
	@echo '  help             - Show this help message and exit. Default if no command is given'


# only necessary b/c in Travis we call `make smt`
.PHONY: coverity
coverity:
	@echo 'Everything is handled by the Coverity add-on in Travis'


LOCAL_DJANGO_PO=tcms/locale/en/LC_MESSAGES/django.po

.PHONY: messages
messages:
	./manage.py makemessages --locale en --no-obsolete \
	    --ignore "test*.py" --ignore "docs/*" --ignore "kiwi_lint/*" \
	    --ignore "*.egg-info/*" --ignore "*/node_modules/*"
	git checkout tcms/locale/eo_UY/

	for APP_NAME in "github-app" "github-marketplace" "enterprise" "tenants" "trackers-integration"; do \
	    echo "---- Trying to merge translations from ../$$APP_NAME"; \
	    if [ -d "../$$APP_NAME" ]; then \
	        REMOTE_DJANGO_PO=`find ../$$APP_NAME -type f -wholename "*/locale/en/LC_MESSAGES/django.po"`; \
	        msgcat --use-first -o $(LOCAL_DJANGO_PO) $(LOCAL_DJANGO_PO) $$REMOTE_DJANGO_PO; \
	    fi; \
	done

	ls tcms/locale/en/LC_MESSAGES/*.po | xargs -n 1 -I @ msgattrib -o @ --no-fuzzy @
