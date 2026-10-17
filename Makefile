PORT ?= 8000
PY ?= python

install:            ## install app + test dependencies and the Playwright browser
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m playwright install --with-deps chromium

run:                ## start the app with auto-reload on http://localhost:$(PORT)
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

run-vuln:           ## start with deliberate security flaws enabled (local only!)
	SANDBOX_VULNS=1 $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

run-bugs:           ## start with planted report bugs enabled
	SANDBOX_BUGS=1 $(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

seed:               ## wipe and reseed the database (LEADS=300 SEED=42)
	$(PY) -m app.seed --reset --leads $(or $(LEADS),300) --seed $(or $(SEED),42)

test:               ## whole suite + HTML report in reports/
	$(PY) -m pytest --html=reports/report.html --self-contained-html

test-api:
	$(PY) -m pytest -m api

test-data:
	$(PY) -m pytest -m data

test-ui:            ## add HEADED=1 to watch the browser
	$(PY) -m pytest -m ui $(if $(HEADED),--headed --slowmo 300,) --tracing retain-on-failure

perf:               ## JMeter non-GUI run + HTML dashboard in perf/results/
	rm -rf perf/results && mkdir -p perf/results
	jmeter -n -t perf/leadflow_load.jmx -Jthreads=$(or $(THREADS),20) -Jduration=$(or $(DURATION),60) \
	  -l perf/results/results.jtl -e -o perf/results/dashboard

.PHONY: install run run-vuln run-bugs seed test test-api test-data test-ui perf
