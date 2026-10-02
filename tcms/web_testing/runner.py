"""Bounded browser execution with per-dataset reusable login state."""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.utils import timezone
from tcms.ai_assistant.crypto import decrypt_api_key
from .models import WebRun, WebResult
from .validation import ACTIONS, target_url, validate_url, validate_steps


class Cancelled(Exception):
    pass


def claim_run():
    with transaction.atomic():
        candidate = WebRun.objects.filter(status='queued', cancel_requested=False).order_by('created').first()
        if candidate and WebRun.objects.filter(pk=candidate.pk, status='queued', cancel_requested=False).update(status='running',started=timezone.now(),heartbeat=timezone.now()):
            return candidate.pk
    return None


def recover_stale():
    return WebRun.objects.filter(status='running',heartbeat__lt=timezone.now()-timedelta(minutes=10)).update(status='interrupted',finished=timezone.now(),error='执行进程已中断，请检查服务后手动重新执行。')


def browser_context(browser, snapshot, storage=None):
    context = browser.new_context(ignore_https_errors=snapshot['ignore_https_errors'], viewport={'width':1366,'height':768}, accept_downloads=False, service_workers='block', storage_state=storage)
    def route_request(route):
        try: validate_url(route.request.url)
        except ValueError: route.abort('blockedbyclient')
        else: route.continue_()
    context.route('**/*', route_request)
    context.route_web_socket('**/*', lambda socket:socket.close())
    return context


def perform(page, steps, snapshot, deadline, checkpoint, logs):
    from playwright.sync_api import expect
    validate_steps(steps)
    for number, step in enumerate(steps, 1):
        checkpoint()
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError
        timeout = min(10000,max(1,int(remaining*1000)))
        page.set_default_timeout(timeout)
        action, value = step['action'], step.get('value','')
        locator = page.locator(step.get('selector') or 'body')
        entry = {'number':number,'action':ACTIONS[action],'status':'failed'}; logs.append(entry)
        if action == 'goto': page.goto(target_url(snapshot['base_url'],value), wait_until='domcontentloaded',timeout=timeout)
        elif action == 'click': locator.click()
        elif action == 'fill': locator.fill(value)
        elif action == 'select': locator.select_option(value)
        elif action == 'check': locator.check()
        elif action == 'assert_visible': expect(locator).to_be_visible(timeout=timeout)
        elif action == 'assert_text': expect(locator).to_contain_text(value,timeout=timeout)
        elif action == 'assert_url': expect(page).to_have_url(re.compile(re.escape(value)),timeout=timeout)
        elif action == 'assert_title': expect(page).to_have_title(re.compile(re.escape(value)),timeout=timeout)
        entry['status']='passed'


def capture(page):
    try:
        image = page.screenshot(full_page=False,timeout=5000)
        return image if len(image)<=2*1024*1024 else None
    except Exception:
        return None


def execute(run_id):
    run = WebRun.objects.get(pk=run_id)
    if run.status != 'running': return
    deadline, final = time.monotonic()+300, 'passed'
    try:
        from playwright.sync_api import sync_playwright
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted)); validate_url(snapshot['base_url'])
        with ThreadPoolExecutor(max_workers=1) as database, sync_playwright() as playwright:
            def db(func,*args,**kwargs): return database.submit(func,*args,**kwargs).result()
            def checkpoint():
                db(run.refresh_from_db,fields=('cancel_requested','status'))
                if run.cancel_requested or run.status != 'running': raise Cancelled
                if time.monotonic() >= deadline: raise TimeoutError
                if not db(lambda:get_user_model().objects.filter(pk=run.owner_id,is_active=True).exists()): raise Cancelled
                db(lambda:WebRun.objects.filter(pk=run_id,status='running').update(heartbeat=timezone.now()))
            browser = None
            try:
                endpoint = os.environ.get('WEB_TEST_BROWSER_WS','')
                browser = playwright.chromium.connect(endpoint,timeout=30000) if endpoint else playwright.chromium.launch(headless=True)
                current_dataset, storage, setup_error, setup_shot = None, None, '', None
                for position, case in enumerate(snapshot['cases'],1):
                    checkpoint()
                    group = case.get('dataset',0)
                    if group != current_dataset:
                        current_dataset, storage, setup_error, setup_shot = group, None, '', None
                        if case.get('setup_steps'):
                            login_context = browser_context(browser,snapshot)
                            page = login_context.new_page()
                            try:
                                perform(page,case['setup_steps'],snapshot,deadline,checkpoint,[])
                                storage = login_context.storage_state()
                            except Cancelled: raise
                            except Exception as exc:
                                setup_error = f'公共登录/前置步骤失败（{type(exc).__name__}），未执行业务步骤。'
                                setup_shot = capture(page)
                            finally: login_context.close()
                    logs, error, screenshot, status = [], setup_error, setup_shot, 'failed' if setup_error else 'passed'
                    started = time.monotonic()
                    if not setup_error:
                        context = browser_context(browser,snapshot,storage)
                        page = context.new_page()
                        try:
                            perform(page,case['steps'],snapshot,deadline,checkpoint,logs)
                        except Cancelled: raise
                        except Exception as exc:
                            status = 'failed'
                            error = f'第 {len(logs)} 步失败（{type(exc).__name__}）。请检查定位器、预期结果与页面截图。'
                            screenshot = capture(page)
                        finally: context.close()
                    if status == 'failed': final='failed'
                    db(WebResult.objects.create,run=run,position=position,name=case['name'],status=status,steps=logs,error=error,screenshot=screenshot,elapsed_ms=int((time.monotonic()-started)*1000))
                    db(lambda:WebRun.objects.filter(pk=run_id,status='running').update(completed_count=position,heartbeat=timezone.now()))
                    if status == 'failed' and snapshot['stop_on_failure']: break
            finally:
                try:
                    if browser: browser.close()
                finally: db(connections.close_all)
    except Cancelled: final='cancelled'
    except Exception as exc:
        final='error'
        WebRun.objects.filter(pk=run_id).update(error=f'执行环境异常（{type(exc).__name__}），请检查浏览器服务和测试站点配置。')
    with transaction.atomic():
        current = WebRun.objects.select_for_update().get(pk=run_id)
        if current.status == 'running':
            current.status = 'cancelled' if current.cancel_requested else final
            current.finished = timezone.now(); current.save(update_fields=('status','finished'))
