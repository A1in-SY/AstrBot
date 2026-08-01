// Utility for managing sidebar customization in localStorage
const STORAGE_KEY = 'astrbot_sidebar_customization';

/**
 * Get the customized sidebar configuration from localStorage
 * @returns {Object|null} The customization config or null if not set
 */
export function getSidebarCustomization() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? JSON.parse(stored) : null;
  } catch (error) {
    console.error('Error reading sidebar customization:', error);
    return null;
  }
}

/**
 * Save the sidebar customization to localStorage
 * @param {Object} config - The customization configuration
 * @param {Array} config.mainItems - Array of item titles for main sidebar
 * @param {Array} config.moreItems - Array of item titles for "More Features" group
 */
export function setSidebarCustomization(config) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch (error) {
    console.error('Error saving sidebar customization:', error);
  }
}

/**
 * Clear the sidebar customization (reset to default)
 */
export function clearSidebarCustomization() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (error) {
    console.error('Error clearing sidebar customization:', error);
  }
}

/**
 * 解析侧边栏默认项与用户定制，返回主区/更多区及可选的合并结果
 * @param {Array} defaultItems - 默认侧边栏结构
 * @param {Object|null} customization - 用户定制（mainItems/moreItems）
 * @param {Object} options
 * @param {boolean} [options.cloneItems=false] - 是否克隆条目以避免外部引用被修改
 * @param {boolean} [options.assembleMoreGroup=false] - 是否组装带更多分组的整体数组
 * @returns {{ mainItems: Array, moreItems: Array, merged?: Array }}
 */
import { MORE_GROUP_KEY } from "@/layouts/full/vertical-sidebar/sidebarItem";

// These entries represent product-level navigation, not user-optional items.
// Keep them in the main section when a saved customization predates a layout
// change (for example, Trace moved out of the old More group).
const REQUIRED_MAIN_ITEM_KEYS = new Set([
  'core.navigation.trace'
]);

export function resolveSidebarItems(defaultItems, customization, options = {}) {
  const { cloneItems = false, assembleMoreGroup = false } = options;

  const normalizeKeys = (keys = []) => {
    const list = Array.isArray(keys) ? keys : [];
    const deduped = [];
    const seen = new Set();

    list.forEach((key) => {
      if (typeof key !== 'string') return;
      if (seen.has(key)) return;
      seen.add(key);
      deduped.push(key);
    });

    return deduped;
  };

  const all = new Map();
  const defaultMain = [];
  const defaultMore = [];

  // 收集所有条目，按 title 建索引
  defaultItems.forEach(item => {
    if (item.children && item.title === MORE_GROUP_KEY) {
      item.children.forEach(child => {
        all.set(child.title, cloneItems ? { ...child } : child);
        defaultMore.push(child.title);
      });
    } else {
      all.set(item.title, cloneItems ? { ...item } : item);
      defaultMain.push(item.title);
    }
  });

  const hasCustomization = Boolean(customization);
  let mainKeys = hasCustomization ? normalizeKeys(customization.mainItems || []) : [...defaultMain];
  let moreKeys = hasCustomization ? normalizeKeys(customization.moreItems || []) : [...defaultMore];

  if (hasCustomization) {
    mainKeys = mainKeys.filter(title => all.has(title));
    moreKeys = moreKeys.filter(title => all.has(title));

    const requiredMainKeys = defaultMain.filter(title => REQUIRED_MAIN_ITEM_KEYS.has(title));
    if (requiredMainKeys.length > 0) {
      const requiredMainKeySet = new Set(requiredMainKeys);
      mainKeys = mainKeys.filter(title => !requiredMainKeySet.has(title));
      moreKeys = moreKeys.filter(title => !requiredMainKeySet.has(title));

      requiredMainKeys.forEach((title) => {
        const defaultIndex = defaultMain.indexOf(title);
        const nextMainKey = defaultMain
          .slice(defaultIndex + 1)
          .find(candidate => mainKeys.includes(candidate));
        if (nextMainKey) {
          mainKeys.splice(mainKeys.indexOf(nextMainKey), 0, title);
          return;
        }

        const previousMainKey = defaultMain
          .slice(0, defaultIndex)
          .reverse()
          .find(candidate => mainKeys.includes(candidate));
        if (previousMainKey) {
          mainKeys.splice(mainKeys.indexOf(previousMainKey) + 1, 0, title);
          return;
        }

        mainKeys.push(title);
      });
    }
  }

  if (hasCustomization) {
    // 如果同一项同时出现在主区与更多区，主区优先。
    const mainSet = new Set(mainKeys);
    moreKeys = moreKeys.filter(title => !mainSet.has(title));
  }

  const used = hasCustomization
    ? new Set([...mainKeys, ...moreKeys])
    : new Set(defaultMain.concat(defaultMore));

  const mainItems = mainKeys
    .map(title => all.get(title))
    .filter(Boolean);

  if (hasCustomization) {
    // 补充新增默认主区项
    defaultMain.forEach(title => {
      if (!used.has(title)) {
        const item = all.get(title);
        if (item) mainItems.push(item);
      }
    });
  }

  const moreItems = moreKeys
    .map(title => all.get(title))
    .filter(Boolean);

  if (hasCustomization) {
    // 补充新增默认更多区项
    defaultMore.forEach(title => {
      if (!used.has(title)) {
        const item = all.get(title);
        if (item) moreItems.push(item);
      }
    });
  }

  let merged;
  if (assembleMoreGroup) {
    const children = cloneItems ? moreItems.map(item => ({ ...item })) : [...moreItems];
    if (children.length > 0) {
      merged = [
        ...mainItems,
        {
          title: MORE_GROUP_KEY,
          icon: 'mdi-dots-horizontal',
          children
        }
      ];
    } else {
      merged = [...mainItems];
    }
  }

  return {
    mainItems,
    moreItems,
    merged,
    normalizedMainKeys: [...mainKeys],
    normalizedMoreKeys: [...moreKeys]
  };
}

/**
 * 应用侧边栏定制，返回包含更多分组的完整结构
 * @param {Array} defaultItems - 默认侧边栏结构
 * @returns {Array} 自定义后的结构（新数组，不修改入参）
 */
export function applySidebarCustomization(defaultItems) {
  const customization = getSidebarCustomization();
  const {
    merged,
    normalizedMainKeys,
    normalizedMoreKeys
  } = resolveSidebarItems(defaultItems, customization, {
    cloneItems: true,
    assembleMoreGroup: true
  });

  if (customization) {
    const rawMainKeys = Array.isArray(customization.mainItems) ? customization.mainItems : [];
    const rawMoreKeys = Array.isArray(customization.moreItems) ? customization.moreItems : [];
    const hasChanged =
      JSON.stringify(rawMainKeys) !== JSON.stringify(normalizedMainKeys) ||
      JSON.stringify(rawMoreKeys) !== JSON.stringify(normalizedMoreKeys);

    if (hasChanged) {
      setSidebarCustomization({
        mainItems: normalizedMainKeys,
        moreItems: normalizedMoreKeys
      });
    }
  }

  return merged || defaultItems;
}
