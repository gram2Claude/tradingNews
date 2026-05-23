# T7.2 probe results

## chromium headless — FAIL
  ok: False
  warmup_ms: 4016
  cookies: ['spid', 'spsc']
  html_size_main: 98388
  is_challenge: False
  search_query_used: None
  x5_company_link: None
  listing_url: None
  events_found_on_listing: None
  page2_exists: None
  first_event_url: None
  first_event_title: None
  first_event_body_len: None
  failure: could not find X5 company link via any search query

## chromium headed — FAIL
  ok: False
  warmup_ms: None
  cookies: []
  html_size_main: None
  is_challenge: None
  search_query_used: None
  x5_company_link: None
  listing_url: None
  events_found_on_listing: None
  page2_exists: None
  first_event_url: None
  first_event_title: None
  first_event_body_len: None
  failure: main goto timeout: Page.goto: Timeout 30000ms exceeded.
Call log:
  - navigating to "https://www.e-disclosure.ru/", waiting until "networkidle"


## firefox headless — FAIL
  ok: False
  warmup_ms: 1463
  cookies: []
  html_size_main: 1752
  is_challenge: True
  search_query_used: None
  x5_company_link: None
  listing_url: None
  events_found_on_listing: None
  page2_exists: None
  first_event_url: None
  first_event_title: None
  first_event_body_len: None
  failure: main page still shows challenge after networkidle

## firefox headed — FAIL
  ok: False
  warmup_ms: 1833
  cookies: []
  html_size_main: 1752
  is_challenge: True
  search_query_used: None
  x5_company_link: None
  listing_url: None
  events_found_on_listing: None
  page2_exists: None
  first_event_url: None
  first_event_title: None
  first_event_body_len: None
  failure: main page still shows challenge after networkidle
