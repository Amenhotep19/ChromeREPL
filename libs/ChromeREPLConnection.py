import sublime
import requests
import re
import time
import ChromeREPL.libs.ChromeREPLHelpers as ChromeREPLHelpers
import ChromeREPL.libs.GotoWindow as GotoWindow
import ChromeREPL.libs.PyChromeDevTools as PyChromeDevTools


class ChromeREPLConnection():
  settings = None
  instances = {}
  STATUS_KEY = 'chrome-repl'

  def get_instance(sublime_view):
    key = sublime_view.id()
    return (ChromeREPLConnection.instances[key]
            if key in ChromeREPLConnection.instances
            else ChromeREPLConnection(sublime_view))

  def close_all_instances():
    for instance in ChromeREPLConnection.instances.values():
      instance.close()

  def clear_statuses():
    for instance in ChromeREPLConnection.instances.values():
      instance.clear_tab_status()

  def is_user_tab(tab):
    is_page = tab['type'] == 'page'
    is_devtools = tab['url'].startswith('chrome-devtools://')
    is_extension = tab['url'].startswith('chrome-extension://')
    is_resource = tab['url'].startswith('res:')

    is_junk = is_devtools or is_extension or is_resource

    return is_page and not is_junk

  def activate_tab(tab_id):
    try:
      settings = ChromeREPLConnection.settings
      url = 'http://{}:{}/json/activate/{}'.format(settings.get('hostname'),
                                                   settings.get('port'),
                                                   tab_id)
      return requests.post(url)
    except requests.exceptions.ConnectionError as e:
      return None

  def __init__(self, sublime_view):
    super(ChromeREPLConnection, self).__init__()
    self.chrome = None
    self.view = sublime_view
    self.tabs = []

    ChromeREPLConnection.instances[sublime_view.id()] = self

  def is_connected(self):
    return (self.chrome is not None and
            ChromeREPLHelpers.is_chrome_running_with_remote_debugging() and
            self.chrome.ws.connected)

  def connect_to_chrome(self):
    if not ChromeREPLHelpers.is_chrome_running_with_remote_debugging():
      return False

    response = ChromeREPLHelpers.request_json_from_chrome()
    if response is None:
      return False

    settings = ChromeREPLConnection.settings
    self.chrome = PyChromeDevTools.ChromeInterface(port=settings.get('port'))
    self.set_tab_status()
    sublime.active_window().run_command('chrome_repl_connect_to_tab')

    return True

  def connect_to_tab(self):
    if not self.is_connected():
      self.connect_to_chrome()
      if not self.is_connected():
        return

    self.chrome.get_tabs()  # this doesn't return tabs, but updates them internally
    self.tabs = [tab for tab in self.chrome.tabs if ChromeREPLConnection.is_user_tab(tab)]
    labels = [tab['title'] for tab in self.tabs]

    def tab_selected(tab_index):
      if tab_index == -1:  # user cancelled
        return

      tab = self.tabs[0] if len(self.tabs) == 1 else self.tabs[tab_index]
      tab_index = self.chrome.tabs.index(tab)
      # not using connect_targetID so that chrome stores the connected tab
      self.chrome.connect(tab_index, False)
      ChromeREPLConnection.activate_tab(tab['id'])
      GotoWindow.focus_window(self.view.window())

      try:
        self.chrome_print("'Sublime Text connected'")
      except BrokenPipeError as e:
        sublime.error_message("Sublime could not connect to tab. Did it close?")

      self.set_tab_status()

      global try_count, connected
      try_count = 0
      connected = False

      def connect():
        time.sleep(0.5)
        global try_count, connected
        if try_count < 5:
          try_count += 1
          try:
            connected = self.connect_to_chrome()
            if not connected:
              connect()
          except requests.exceptions.ConnectionError as e:
            connect()
        else:
          connected = False

      connect()

      if not connected:
        sublime.error_message("Failed to connect to Chrome")

      return connected

    self.view.window().show_quick_panel(labels, tab_selected)

  def chrome_evaluate(self, expression):
    if not self.is_connected():
      return

    settings = ChromeREPLConnection.settings
    includeCommandLineAPI = settings.get('include_command_line_api', False)
    response = self.chrome.Runtime.evaluate(expression=expression,
                                            objectGroup='console',
                                            includeCommandLineAPI=includeCommandLineAPI,
                                            silent=False,
                                            returnByValue=False,
                                            generatePreview=False)
    # print(response)
    return response

  def chrome_print(self, expression, method='log', prefix='', color='rgb(150, 150, 150)'):
    expression = expression.strip()
    if expression[-1] == ';':
      expression = expression[0:-1]

    log_expression = 'console.{}(`%cST {}`, "color:{};", {})'.format(method,
                                                                     prefix,
                                                                     color,
                                                                     expression)
    self.chrome_evaluate(log_expression)

  def execute(self, expression):
    try:
      # print the expression to the console as a string
      print_expression = '`{}`'.format(expression)
      self.chrome_print(expression=print_expression, prefix=' in:')
    except BrokenPipeError as e:
      print("broken pipe error")

    # translated from
    # https://github.com/chromium/chromium/blob/master/third_party/WebKit/Source/devtools/front_end/sdk/RuntimeModel.js
    def wrap_object_literal_expression_if_needed(code):
      starts_like_object = re.search('^\s*\{', code) is not None
      ends_like_object = re.search('\}\s*$', code) is not None

      if not (starts_like_object and ends_like_object):
        return code

      # try parsing as an expression and check for errors
      # will throw a Syntax Error if not object literal
      def create_parse_expression(code):
        return '(async () => 0).constructor(`return {};`)'.format(code)

      unwrapped_result = self.chrome_evaluate(create_parse_expression(code))

      wrapped_code = '({})'.format(code)
      wrapped_result = self.chrome_evaluate(create_parse_expression(wrapped_code))

      is_object = ('exceptionDetails' not in unwrapped_result['result'].keys() and
                   'exceptionDetails' not in wrapped_result['result'].keys())

      return wrapped_code if is_object else code

    # evaluate the expression
    evaluate_expression = wrap_object_literal_expression_if_needed(expression)
    response = self.chrome_evaluate(evaluate_expression)

    # print the result to the Chrome console as a string
    if response is not None:
      result = response['result']['result']

      if 'exceptionDetails' in response['result'].keys():
        method = 'error'
        print_text = '`{}`'.format(response['result']['exceptionDetails']['exception']['description'])
      elif 'description' in result.keys() and 'value' not in result.keys():
        method = 'log'
        print_text = expression
      elif 'value' in result.keys() and result['value'] is not None:
        method = 'log'
        template = '`"{}"`' if result['type'] == 'string' else '`{}`'
        value = str(result['value']).lower() if result['type'] == 'boolean' else result['value']
        print_text = template.format(value)
      elif 'subtype' in result.keys():
        method = 'log'
        print_text = '`{}`'.format(result['subtype'])
      elif 'type' in result.keys():
        method = 'log'
        print_text = '`{}`'.format(result['type'])
      else:
        # shouldn't reach here, included for debug
        method = 'log'
        print_text = expression

      self.chrome_print(expression=print_text, method=method, prefix='out:')

  def reload(self, ignoreCache=False):
    self.chrome.Page.reload(args={'ignoreCache': ignoreCache})

  def set_tab_status(self):
    if self.chrome is None:
      return

    status = 'ChromeREPL Tab: {}'.format(self.chrome.current_tab['title'])
    self.view.set_status(ChromeREPLConnection.STATUS_KEY, status)

  def clear_tab_status(self):
    self.view.erase_status(ChromeREPLConnection.STATUS_KEY)

  def close(self):
    if self.chrome is not None:
      self.chrome.close()
