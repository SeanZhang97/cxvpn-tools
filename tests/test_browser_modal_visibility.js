const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class ClassList {
  constructor(...names) {
    this.names = new Set(names);
  }

  add(name) {
    this.names.add(name);
  }

  remove(name) {
    this.names.delete(name);
  }

  contains(name) {
    return this.names.has(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.contains(name) : force;
    if (enabled) this.add(name);
    else this.remove(name);
    return enabled;
  }
}

const elements = new Map([
  ['main-content', { classList: new ClassList() }],
  ['capmodal', { classList: new ClassList('hidden') }],
  ['smsmodal', { classList: new ClassList('hidden') }],
]);
const visibilityCalls = [];
const document = {
  body: {},
  addEventListener() {},
  getElementById(id) {
    return elements.get(id);
  },
  querySelectorAll() {
    return [];
  },
};
const context = {
  clearTimeout,
  console,
  document,
  MutationObserver: class {
    observe() {}
  },
  setInterval() {},
  setTimeout,
  window: {
    addEventListener() {},
    pywebview: {
      api: {
        async browser_set_visible(visible) {
          visibilityCalls.push(visible);
          return true;
        },
      },
    },
  },
};

vm.createContext(context);
const appPath = path.join(__dirname, '..', 'ui', 'app.js');
vm.runInContext(fs.readFileSync(appPath, 'utf8'), context, { filename: appPath });

async function run() {
  vm.runInContext("curPage = 'browser'", context);

  await vm.runInContext('syncBrowserVisibility()', context);
  await vm.runInContext("showBlockingBrowserModal($('capmodal'))", context);
  assert.equal(elements.get('capmodal').classList.contains('hidden'), false);

  await vm.runInContext('syncBrowserVisibility()', context);
  await vm.runInContext("showBlockingBrowserModal($('smsmodal'))", context);
  await vm.runInContext("hideBlockingBrowserModal($('capmodal'))", context);
  assert.equal(elements.get('smsmodal').classList.contains('hidden'), false);

  await vm.runInContext("hideBlockingBrowserModal($('smsmodal'))", context);
  assert.deepEqual(visibilityCalls, [true, false, false, false, false, true]);
  console.log('browser modal visibility coordination: ok');
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
