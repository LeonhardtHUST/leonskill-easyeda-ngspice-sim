const tree = await eda.dmt_EditorControl.getSplitScreenTree();
const tabs = tree?.tabs || [];
const simTab = tabs.find(t => t.data?.doctype === 8);
const pages = tabs.filter(t => t.data?.doctype === 1).map(t => ({ title: t.title, tabId: t.tabId }));
return JSON.stringify({
  hasSimTab: !!simTab,
  simTabTitle: simTab ? simTab.title : null,
  simTabId: simTab ? simTab.tabId : null,
  openSchematics: pages
});
