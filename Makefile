PY        ?= python3
BIN       ?= ./bin/jxtest
SRC       ?= examples/petstore/openapi.yaml
BASE      ?= $(API_BASE_URL)
ENV       ?= dev
PORT      ?= 8080

OUT_SPEC     = api-spec.json
OUT_CASES    = test-cases.json
OUT_RESULTS  = test-results.json
OUT_REPORT   = report.html
OUT_DOCS     = docs.md
OUT_HEAL     = test-heal-report.json
OUT_SECURITY = test-security-results.json
OUT_DIFF     = migration.md
OUT_COVERAGE = coverage.md

.PHONY: help install test env-create schema gen validate run load heal report doc mock security diff coverage ci all clean

help:
	@echo "Targets:"
	@echo "  make install                       symlink bin/jxtest into ~/.local/bin (POSIX only)"
	@echo "  make test                          run the test suite"
	@echo "  make env-create NAME=dev URL=...   create env file"
	@echo "  make schema SRC=<file]             parse API spec"
	@echo "  make gen                           generate test cases"
	@echo "  make validate                      validate test-cases.json"
	@echo "  make run [ENV=dev] [BASE=url]      execute tests"
	@echo "  make load [BASE=url] [SLA=...]     load test + SLA"
	@echo "  make heal                          auto-fix failures"
	@echo "  make security                      run OWASP API probes"
	@echo "  make diff OLD=<f> NEW=<f>          compare two specs"
	@echo "  make coverage                      analyze test coverage gaps"
	@echo "  make report                        render HTML report"
	@echo "  make doc                           generate Markdown docs"
	@echo "  make mock [PORT=8080]              start mock server"
	@echo "  make ci [BASE=url] [SLA=p95<500]   CI pipeline: gen+run+load+security"
	@echo "  make all                           full pipeline"
	@echo "  make clean                         remove generated files"
	@echo ""
	@echo "Or use the CLI directly:  jxtest <schema|gen|run|load|heal|security|diff|report|doc|env|mock|scenario|factory|completion>"

install:
	@mkdir -p ~/.local/bin
	@ln -sf "$(abspath $(BIN))" ~/.local/bin/jxtest
	@echo "OK  installed: ~/.local/bin/jxtest -> $(BIN)"
	@echo "    ensure ~/.local/bin is in PATH"
	@echo "    (this target is POSIX-only; on Windows run: python bin\\jxtest install)"

test:
	@for t in tests/test_*.py; do echo "== $$t"; python3 "$$t" || exit 1; done
	@echo "OK  all tests passed"

env-create:
	@test -n "$(NAME)" || (echo "ERROR: NAME=<env-name> required" && exit 1)
	@test -n "$(URL)" || (echo "ERROR: URL=<base-url> required" && exit 1)
	$(BIN) env create $(NAME) --base-url $(URL)

schema:
	$(BIN) schema $(SRC) -o $(OUT_SPEC)

gen:
	$(BIN) gen $(OUT_SPEC) -o $(OUT_CASES)

validate:
	$(BIN) validate $(OUT_CASES) --spec $(OUT_SPEC)

run:
	@test -n "$(BASE)" || (echo "ERROR: BASE=... or API_BASE_URL=... required" && exit 1)
	$(BIN) run $(OUT_CASES) --env $(ENV) --base-url $(BASE) -o $(OUT_RESULTS) --junit

heal:
	$(BIN) heal $(OUT_RESULTS) --cases $(OUT_CASES) --spec $(OUT_SPEC) --report $(OUT_HEAL)

report:
	$(BIN) report $(OUT_RESULTS) -o $(OUT_REPORT)

doc:
	$(BIN) doc $(OUT_SPEC) --results $(OUT_RESULTS) -o $(OUT_DOCS)

mock:
	$(BIN) mock $(OUT_SPEC) --port $(PORT)

security:
	@test -n "$(BASE)" || (echo "ERROR: BASE=... or API_BASE_URL=... required" && exit 1)
	$(BIN) security $(OUT_SPEC) --base-url $(BASE) -o $(OUT_SECURITY)

diff:
	@test -n "$(OLD)" || (echo "ERROR: OLD=<old-spec> required" && exit 1)
	@test -n "$(NEW)" || (echo "ERROR: NEW=<new-spec> required" && exit 1)
	$(BIN) diff $(OLD) $(NEW) -o $(OUT_DIFF)

coverage:
	$(BIN) coverage $(OUT_RESULTS) --spec $(OUT_SPEC) -o $(OUT_COVERAGE)

ci: schema gen validate run load security coverage report doc
	@echo "OK  CI pipeline complete"

all: schema gen validate run load heal security report doc
	@echo "OK  pipeline complete. open $(OUT_REPORT)"

clean:
	rm -f $(OUT_SPEC) $(OUT_CASES) $(OUT_RESULTS) $(OUT_REPORT) $(OUT_DOCS) $(OUT_HEAL) $(OUT_SECURITY) $(OUT_DIFF) $(OUT_COVERAGE) test-results.xml test-load-results.json
	rm -rf responses
