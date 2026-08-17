'use strict';

/*
 * FGO story hook (IL2CPP, arm64).
 * This script is loaded by the arm64 Frida Gadget running through LDPlayer's
 * NativeBridge/Houdini layer.  Do not load it through the x86_64 server.
 */

const state = {
  speakerByManager: new Map(),
  globalSpeaker: '',
  logSpeaker: '',
  lastTextByManager: new Map(),
  pendingChunks: new Map(),
  choicesByDialog: new Map(),
  lastChoiceList: null,
  lastChoiceSelection: null,
  lastBacklogSignature: '',
  lastScenarioPlanSignature: '',
  scenarioSpeakers: new Map(),
  backlogTimer: null,
  installTimer: null,
  installAttempts: 0,
  installComplete: false,
  installed: [],
};
const SCENARIO_ARRAY_LIMIT = 16384;
const SCENARIO_MESSAGE_LIMIT = 4096;
const INSTALL_RETRY_MS = 300;
const INSTALL_MAX_ATTEMPTS = 400;

function report(kind, extra) {
  send(Object.assign({ type: kind }, extra || {}));
}

function fail(where, error) {
  report('diagnostic', {
    level: 'error',
    where: where,
    message: String(error),
    stack: error && error.stack ? String(error.stack) : '',
  });
}

function scheduleInstallRetry(reason) {
  if (state.installComplete || state.installTimer !== null) return;
  state.installAttempts += 1;
  if (state.installAttempts === 1 || state.installAttempts % 20 === 0) {
    report('status', {
      ready: false,
      text: '正在等待 FGO Unity 运行时加载（自动重试，无需操作）…',
      detail: String(reason || ''),
    });
  }
  if (state.installAttempts >= INSTALL_MAX_ATTEMPTS) {
    fail(
      'install',
      new Error('等待 FGO Unity 运行时超时：' + String(reason || 'libil2cpp.so 未就绪'))
    );
    return;
  }
  state.installTimer = setTimeout(function () {
    state.installTimer = null;
    install();
  }, INSTALL_RETRY_MS);
}

function install() {
  if (state.installComplete) return;
  try {
    // Gadget may become reachable a little earlier than Unity finishes loading
    // libil2cpp.so. Treat that as a normal startup state instead of a permanent
    // Hook failure.
    const il2cpp = Process.findModuleByName('libil2cpp.so');
    if (il2cpp === null) {
      scheduleInstallRetry('libil2cpp.so 尚未加载');
      return;
    }
    const exportsByName = new Map(
      il2cpp.enumerateExports().map(function (item) { return [item.name, item.address]; })
    );

    function api(name, ret, args) {
      const address = exportsByName.get(name);
      if (!address) throw new Error('missing IL2CPP export: ' + name);
      return new NativeFunction(address, ret, args);
    }

    const domainGet = api('il2cpp_domain_get', 'pointer', []);
    const threadAttach = api('il2cpp_thread_attach', 'pointer', ['pointer']);
    const domainGetAssemblies = api(
      'il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']
    );
    const assemblyGetImage = api('il2cpp_assembly_get_image', 'pointer', ['pointer']);
    const imageGetName = api('il2cpp_image_get_name', 'pointer', ['pointer']);
    const classFromName = api(
      'il2cpp_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']
    );
    const classGetMethod = api(
      'il2cpp_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']
    );
    const classGetType = api('il2cpp_class_get_type', 'pointer', ['pointer']);
    const typeGetObject = api('il2cpp_type_get_object', 'pointer', ['pointer']);
    const objectGetClass = api('il2cpp_object_get_class', 'pointer', ['pointer']);
    const classGetName = api('il2cpp_class_get_name', 'pointer', ['pointer']);
    const classGetField = api(
      'il2cpp_class_get_field_from_name', 'pointer', ['pointer', 'pointer']
    );
    const fieldGetOffset = api('il2cpp_field_get_offset', 'uint32', ['pointer']);
    const fieldStaticGetValue = api(
      'il2cpp_field_static_get_value', 'void', ['pointer', 'pointer']
    );
    const stringLength = api('il2cpp_string_length', 'int', ['pointer']);
    const stringChars = api('il2cpp_string_chars', 'pointer', ['pointer']);

    const domain = domainGet();
    if (domain.isNull()) {
      scheduleInstallRetry('IL2CPP domain 尚未初始化');
      return;
    }
    threadAttach(domain);

    const countPtr = Memory.alloc(Process.pointerSize);
    if (Process.pointerSize === 8) countPtr.writeU64(0);
    else countPtr.writeU32(0);
    const assemblies = domainGetAssemblies(domain, countPtr);
    const count = Process.pointerSize === 8
      ? Number(countPtr.readU64())
      : countPtr.readU32();
    let image = ptr(0);
    const imagesByName = new Map();
    for (let i = 0; i < count; i++) {
      const assembly = assemblies.add(i * Process.pointerSize).readPointer();
      const candidate = assemblyGetImage(assembly);
      const namePtr = imageGetName(candidate);
      const name = namePtr.isNull() ? '' : namePtr.readCString();
      imagesByName.set(name, candidate);
      if (name === 'Assembly-CSharp.dll') {
        image = candidate;
      }
    }
    if (image.isNull()) {
      scheduleInstallRetry('Assembly-CSharp.dll 尚未加载');
      return;
    }

    function readManagedString(value) {
      if (!value || value.isNull()) return '';
      try {
        const length = stringLength(value);
        if (length <= 0) return '';
        if (length > 1024 * 1024) return '';
        return stringChars(value).readUtf16String(length);
      } catch (_) {
        return '';
      }
    }

    function readManagedStringArray(value, maxLength) {
      if (!value || value.isNull()) return [];
      try {
        const length = value.add(0x18).readU32();
        const limit = maxLength || 128;
        if (length > limit) return [];
        const result = [];
        for (let i = 0; i < length; i++) {
          const item = value.add(0x20 + i * Process.pointerSize).readPointer();
          result.push(readManagedString(item));
        }
        return result;
      } catch (_) {
        return [];
      }
    }

    function readManagedBoolArray(value, maxLength) {
      if (!value || value.isNull()) return [];
      try {
        const length = value.add(0x18).readU32();
        if (length > (maxLength || 4096)) return [];
        const result = [];
        for (let i = 0; i < length; i++) {
          result.push(value.add(0x20 + i).readU8() !== 0);
        }
        return result;
      } catch (_) {
        return [];
      }
    }

    function findMethod(className, methodName, argumentCount) {
      return findMethodIn(image, '', className, methodName, argumentCount);
    }

    function findMethodIn(targetImage, namespaceName, className, methodName, argumentCount) {
      const ns = Memory.allocUtf8String('');
      const cn = Memory.allocUtf8String(className);
      const mn = Memory.allocUtf8String(methodName);
      const klass = classFromName(
        targetImage,
        Memory.allocUtf8String(namespaceName || ''),
        cn
      );
      if (klass.isNull()) throw new Error('class not found: ' + className);
      const info = classGetMethod(klass, mn, argumentCount);
      if (info.isNull()) {
        throw new Error('method not found: ' + className + '.' + methodName + '/' + argumentCount);
      }
      const fn = info.readPointer();
      if (fn.isNull()) throw new Error('method pointer is null: ' + className + '.' + methodName);
      return { address: fn, info: info };
    }

    function findClass(targetImage, namespaceName, className) {
      const klass = classFromName(
        targetImage,
        Memory.allocUtf8String(namespaceName || ''),
        Memory.allocUtf8String(className)
      );
      if (klass.isNull()) throw new Error('class not found: ' + namespaceName + '.' + className);
      return klass;
    }

    function fieldOffset(klass, fieldName) {
      const field = fieldInfo(klass, fieldName);
      return fieldGetOffset(field);
    }

    function fieldInfo(klass, fieldName) {
      const field = classGetField(klass, Memory.allocUtf8String(fieldName));
      if (field.isNull()) throw new Error('field not found: ' + fieldName);
      return field;
    }

    const scriptManagerClass = findClass(image, '', 'ScriptManager');
    const questFields = {
      questTitle: fieldInfo(scriptManagerClass, 'questTitle'),
      questId: fieldInfo(scriptManagerClass, 'questId'),
      questPhase: fieldInfo(scriptManagerClass, 'questPhase'),
      chapterSubTitle: fieldInfo(scriptManagerClass, 'chapterSubTitle'),
      warId: fieldInfo(scriptManagerClass, 'warId'),
      eventId: fieldInfo(scriptManagerClass, 'eventId'),
      playScriptDataName: fieldInfo(scriptManagerClass, 'playScriptDataName'),
    };
    const scenarioFields = {
      executeTagList: fieldOffset(scriptManagerClass, 'executeTagList'),
      executeDataList: fieldOffset(scriptManagerClass, 'executeDataList'),
    };
    let questMasterObject = null;
    let questEntityGetter = null;
    let questEntityNameOffset = null;
    const questNameCache = new Map();

    function readStaticPointer(field) {
      const output = Memory.alloc(Process.pointerSize);
      output.writePointer(ptr(0));
      fieldStaticGetValue(field, output);
      return output.readPointer();
    }

    function readStaticInt(field) {
      const output = Memory.alloc(4);
      output.writeS32(0);
      fieldStaticGetValue(field, output);
      return output.readS32();
    }

    function questNameFromMaster(questId) {
      if (questId <= 0) return '';
      if (questNameCache.has(questId)) return questNameCache.get(questId);
      try {
        if (questMasterObject === null) {
          const coreImage = imagesByName.get('UnityEngine.CoreModule.dll');
          const dataManagerClass = findClass(image, '', 'DataManager');
          const findAll = findMethodIn(
            coreImage, 'UnityEngine', 'Resources', 'FindObjectsOfTypeAll', 1
          );
          const findAllFn = new NativeFunction(
            findAll.address, 'pointer', ['pointer', 'pointer']
          );
          const managers = findAllFn(
            typeGetObject(classGetType(dataManagerClass)), findAll.info
          );
          questMasterObject = ptr(0);
          if (!managers.isNull() && managers.add(0x18).readU32() > 0) {
            const manager = managers.add(0x20).readPointer();
            const masters = manager.add(fieldOffset(dataManagerClass, 'datalist')).readPointer();
            if (!masters.isNull()) {
              const count = masters.add(0x18).readU32();
              for (let i = 0; i < count; i++) {
                const candidate = masters.add(0x20 + i * Process.pointerSize).readPointer();
                if (candidate.isNull()) continue;
                const namePointer = classGetName(objectGetClass(candidate));
                const className = namePointer.isNull() ? '' : namePointer.readCString();
                if (className === 'QuestMaster') {
                  questMasterObject = candidate;
                  break;
                }
              }
            }
          }
          const getter = findMethod('QuestMaster', 'getQuestEntity', 1);
          questEntityGetter = {
            fn: new NativeFunction(
              getter.address, 'pointer', ['pointer', 'int', 'pointer']
            ),
            info: getter.info,
          };
          questEntityNameOffset = fieldOffset(
            findClass(image, '', 'QuestEntity'), 'name'
          );
        }
        if (!questMasterObject || questMasterObject.isNull()) return '';
        const entity = questEntityGetter.fn(
          questMasterObject, questId, questEntityGetter.info
        );
        if (entity.isNull()) return '';
        const name = cleanForTransport(
          readManagedString(entity.add(questEntityNameOffset).readPointer())
        );
        questNameCache.set(questId, name);
        return name;
      } catch (_) {
        return '';
      }
    }

    function questContext() {
      try {
        const questId = readStaticInt(questFields.questId);
        return {
          title: cleanForTransport(readManagedString(readStaticPointer(questFields.questTitle))),
          master_title: questNameFromMaster(questId),
          quest_id: questId,
          phase: readStaticInt(questFields.questPhase),
          chapter: cleanForTransport(
            readManagedString(readStaticPointer(questFields.chapterSubTitle))
          ),
          war_id: readStaticInt(questFields.warId),
          event_id: readStaticInt(questFields.eventId),
          script: cleanForTransport(
            readManagedString(readStaticPointer(questFields.playScriptDataName))
          ),
        };
      } catch (_) {
        return {};
      }
    }

    function managerKey(value) {
      return value && !value.isNull() ? value.toString() : 'global';
    }

    function speakerFor(manager) {
      return state.speakerByManager.get(managerKey(manager)) || state.globalSpeaker || '';
    }

    function cleanForTransport(value) {
      if (!value) return '';
      return value.replace(/\u0000/g, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    }

    function isControlText(value) {
      const compact = cleanForTransport(value).replace(/\s+/g, '').toLowerCase();
      return compact === 'select' || compact === '[ff4040]select';
    }

    function emitDialogue(manager, rawText, source) {
      const text = cleanForTransport(rawText);
      if (!text || !text.trim()) return;
      if (isControlText(text)) return;
      const pageBreaks = (text.match(/\[(?:r|n|br)\]/gi) || []).length;
      // Opening the in-game LOG can replay a large scenario buffer through
      // AddText. It is not one dialogue page; the exact backlog is read from
      // ScriptBackLog.logData instead.
      if (text.length > 1200 || pageBreaks > 10) return;
      const key = managerKey(manager);
      const previous = state.lastTextByManager.get(key);
      const now = Date.now();
      if (previous && previous.text === text && now - previous.time < 1500) return;
      state.lastTextByManager.set(key, { text: text, time: now });
      report('dialogue', {
        speaker: cleanForTransport(speakerFor(manager)),
        text: text,
        source: source,
        captured_at_ms: now,
        quest: questContext(),
      });
    }

    function emitDialoguePreview(manager, rawText, source) {
      const text = cleanForTransport(rawText);
      if (!text || !text.trim() || isControlText(text)) return;
      const pageBreaks = (text.match(/\[(?:r|n|br)\]/gi) || []).length;
      if (text.length > 1200 || pageBreaks > 10) return;
      const previous = state.lastTextByManager.get(managerKey(manager));
      if (previous && previous.text === text && Date.now() - previous.time < 1500) return;
      report('dialogue_preview', {
        speaker: cleanForTransport(speakerFor(manager)),
        text: text,
        source: source,
        captured_at_ms: Date.now(),
        quest: questContext(),
      });
    }

    function emitChoices(dialog, array, mode, source) {
      const choices = readManagedStringArray(array).map(cleanForTransport);
      if (!choices.length || !choices.some(function (item) { return item.trim(); })) return false;
      const dialogId = managerKey(dialog);
      const now = Date.now();
      const signature = dialogId + '\u0000' + choices.join('\u0000');
      if (
        state.lastChoiceList && state.lastChoiceList.signature === signature &&
        now - state.lastChoiceList.time < 1500
      ) {
        return false;
      }
      state.lastChoiceList = { signature: signature, time: now };
      state.choicesByDialog.set(dialogId, { choices: choices, mode: mode });
      report('choices', {
        dialog_id: dialogId,
        choices: choices,
        mode: mode,
        source: source,
        captured_at_ms: now,
        quest: questContext(),
      });
      return true;
    }

    function emitChoiceSelected(dialog, index, source) {
      const dialogId = managerKey(dialog);
      const current = state.choicesByDialog.get(dialogId);
      const choices = current ? current.choices : [];
      const now = Date.now();
      const signature = dialogId + ':' + index;
      if (
        state.lastChoiceSelection && state.lastChoiceSelection.signature === signature &&
        now - state.lastChoiceSelection.time < 1500
      ) {
        return;
      }
      state.lastChoiceSelection = { signature: signature, time: now };
      report('choice_selected', {
        dialog_id: dialogId,
        index: index,
        text: index >= 0 && index < choices.length ? choices[index] : '',
        mode: current ? current.mode : '',
        source: source,
        captured_at_ms: now,
        quest: questContext(),
      });
    }

    function stripLogColor(value) {
      return cleanForTransport(value).replace(/\[[0-9A-Fa-f]{6,8}\]/g, '');
    }

    function snapshotBacklog() {
      try {
        // Timer callbacks can run outside the thread used during install.
        // Attaching is idempotent and makes managed object access reliable.
        threadAttach(domain);
        const coreImage = imagesByName.get('UnityEngine.CoreModule.dll');
        if (!coreImage) return 0;
        const backLogClass = findClass(image, '', 'ScriptBackLog');
        const labelClass = findClass(image, '', 'ScriptMessageLabel');
        const findAll = findMethodIn(
          coreImage, 'UnityEngine', 'Resources', 'FindObjectsOfTypeAll', 1
        );
        const findAllFn = new NativeFunction(
          findAll.address, 'pointer', ['pointer', 'pointer']
        );
        const objects = findAllFn(typeGetObject(classGetType(scriptManagerClass)), findAll.info);
        if (objects.isNull()) return 0;
        const managerCount = objects.add(0x18).readU32();
        const backLogOffset = fieldOffset(scriptManagerClass, 'backLogDialog');
        const logDataOffset = fieldOffset(backLogClass, 'logData');
        const mainTextOffset = fieldOffset(labelClass, 'mainText');
        const mainPositionOffset = fieldOffset(labelClass, 'mainPosition');
        const entries = [];

        for (let m = 0; m < managerCount; m++) {
          const manager = objects.add(0x20 + m * Process.pointerSize).readPointer();
          if (manager.isNull()) continue;
          const backLog = manager.add(backLogOffset).readPointer();
          if (backLog.isNull()) continue;
          const list = backLog.add(logDataOffset).readPointer();
          if (list.isNull()) continue;
          const size = list.add(0x18).readU32();
          const items = list.add(0x10).readPointer();
          if (!size || size > 4096 || items.isNull()) continue;

          let pendingSpeaker = '';
          let pendingSelect = false;
          let active = null;
          for (let i = 0; i < size; i++) {
            const label = items.add(0x20 + i * Process.pointerSize).readPointer();
            if (label.isNull()) continue;
            const raw = readManagedString(label.add(mainTextOffset).readPointer());
            const text = stripLogColor(raw);
            const y = label.add(mainPositionOffset + 4).readFloat();
            if (!text) continue;

            if (active !== null) {
              if (text === '」') {
                const body = active.parts.join('').trim();
                if (body) {
                  entries.push({ speaker: active.speaker, text: body, kind: 'dialogue' });
                }
                active = null;
                continue;
              }
              if (active.lastY !== null && Math.abs(y - active.lastY) > 1.0) {
                active.parts.push('\n');
              }
              active.parts.push(text);
              active.lastY = y;
              continue;
            }

            if (text.trim().toLowerCase() === 'select') {
              pendingSelect = true;
              pendingSpeaker = '';
              continue;
            }
            if (pendingSelect) {
              const selectedText = text.trim();
              if (selectedText) {
                entries.push({ speaker: '【已选择】', text: selectedText, kind: 'choice_selected' });
              }
              pendingSelect = false;
              continue;
            }
            if (text === '「') {
              active = { speaker: pendingSpeaker, parts: [], lastY: null };
              pendingSpeaker = '';
              continue;
            }
            if (text !== '」') pendingSpeaker = text.trim();
          }
          break;
        }

        const signature = JSON.stringify({ quest: questContext(), entries: entries });
        if (entries.length && signature !== state.lastBacklogSignature) {
          state.lastBacklogSignature = signature;
          report('backlog', {
            entries: entries,
            source: 'ScriptBackLog.logData',
            captured_at_ms: Date.now(),
            quest: questContext(),
          });
        }
        return entries.length;
      } catch (error) {
        report('diagnostic', {
          level: 'warning',
          where: 'snapshotBacklog',
          message: String(error),
        });
        return 0;
      }
    }

    function scheduleBacklogSnapshot(delay) {
      if (state.backlogTimer !== null) clearTimeout(state.backlogTimer);
      state.backlogTimer = setTimeout(function () {
        state.backlogTimer = null;
        snapshotBacklog();
      }, delay || 280);
    }

    function snapshotScenarioPlan(manager) {
      try {
        if (!manager || manager.isNull()) return 0;
        const tags = readManagedStringArray(
          manager.add(scenarioFields.executeTagList).readPointer(), SCENARIO_ARRAY_LIMIT
        );
        const data = readManagedStringArray(
          manager.add(scenarioFields.executeDataList).readPointer(), SCENARIO_ARRAY_LIMIT
        );
        const length = Math.min(tags.length, data.length);
        if (!length) return 0;
        const speakers = state.scenarioSpeakers.get(managerKey(manager)) || new Map();
        const entries = [];

        function isScenarioControlParameter(value) {
          const compact = cleanForTransport(value).trim();
          if (!compact) return true;
          return /^(?:time\s+\d|[#]?[A-Z](?:\s|$|[:,：])|normal$|select$)/i.test(compact);
        }

        // AnalysScript has already expanded the complete stage into parallel
        // tag/data arrays. Commands always have a non-empty tag; visible prose
        // occupies the empty-tag slots. executeMessageFlagList only describes
        // the currently active message ranges and therefore cannot be used to
        // enumerate the full stage.
        let currentSpeaker = '';
        for (let i = 0; i < length; i++) {
          if (speakers.has(i)) {
            currentSpeaker = speakers.get(i) || '';
          }
          const tag = cleanForTransport(tags[i]).trim();
          if (tag) continue;
          const text = cleanForTransport(data[i]);
          if (
            !text || !text.trim() || isControlText(text) ||
            isScenarioControlParameter(text) || text.length > 2400
          ) continue;
          entries.push({
            index: i,
            speaker: cleanForTransport(currentSpeaker),
            text: text,
            tag: '',
            kind: 'dialogue_fragment',
          });
          if (entries.length >= SCENARIO_MESSAGE_LIMIT) break;
        }
        if (!entries.length) return 0;
        const quest = questContext();
        const signature = JSON.stringify({ quest: quest, entries: entries });
        if (signature === state.lastScenarioPlanSignature) return entries.length;
        state.lastScenarioPlanSignature = signature;
        report('scenario_plan', {
          entries: entries,
          source: 'ScriptManager.VisibleTextSlots',
          captured_at_ms: Date.now(),
          quest: quest,
        });
        return entries.length;
      } catch (error) {
        report('diagnostic', {
          level: 'warning',
          where: 'snapshotScenarioPlan',
          message: String(error),
        });
        return 0;
      }
    }

    function hook(className, methodName, argumentCount, callbacks) {
      const method = findMethod(className, methodName, argumentCount);
      Interceptor.attach(method.address, callbacks);
      const label = className + '.' + methodName + '/' + argumentCount;
      state.installed.push(label);
      report('diagnostic', {
        level: 'debug',
        where: label,
        message: 'hooked at ' + method.address.sub(il2cpp.base),
      });
      return method;
    }

    function snapshotCurrentDialogue() {
      try {
        const coreImage = imagesByName.get('UnityEngine.CoreModule.dll');
        if (!coreImage) return false;
        const managerClass = scriptManagerClass;
        const commonClass = findClass(image, '', 'ScriptMessageCommonManager');
        const labelClass = findClass(image, '', 'ScriptMessageLabel');
        const resourcesClass = findClass(coreImage, 'UnityEngine', 'Resources');
        const findAll = findMethodIn(
          coreImage, 'UnityEngine', 'Resources', 'FindObjectsOfTypeAll', 1
        );
        const findAllFn = new NativeFunction(
          findAll.address, 'pointer', ['pointer', 'pointer']
        );
        const managerType = typeGetObject(classGetType(managerClass));
        const objects = findAllFn(managerType, findAll.info);
        if (objects.isNull()) return false;
        const length = objects.add(0x18).readU32();
        if (!length) return false;

        const managerOffset = fieldOffset(managerClass, 'messageManager');
        const selectDialogOffset = fieldOffset(managerClass, 'selectDialog');
        const menuMessageListOffset = fieldOffset(managerClass, 'menuMessageList');
        const selectDialogClass = findClass(image, '', 'ScriptSelectDialog');
        const selectIsOpenOffset = fieldOffset(selectDialogClass, 'isOpen');
        const nameOffset = fieldOffset(commonClass, 'talkNameOnly');
        const labelsOffset = fieldOffset(commonClass, 'dispLabelList');
        const mainTextOffset = fieldOffset(labelClass, 'mainText');
        let captured = false;
        for (let i = 0; i < length; i++) {
          const scriptManager = objects.add(0x20 + i * Process.pointerSize).readPointer();
          if (scriptManager.isNull()) continue;
          snapshotScenarioPlan(scriptManager);

          const selectDialog = scriptManager.add(selectDialogOffset).readPointer();
          if (!selectDialog.isNull() && selectDialog.add(selectIsOpenOffset).readU8() !== 0) {
            const choiceArray = scriptManager.add(menuMessageListOffset).readPointer();
            if (emitChoices(selectDialog, choiceArray, 'snapshot', 'CurrentChoiceSnapshot')) {
              captured = true;
            }
          }

          const messageManager = scriptManager.add(managerOffset).readPointer();
          if (messageManager.isNull()) continue;
          const list = messageManager.add(labelsOffset).readPointer();
          if (list.isNull()) continue;
          const size = list.add(0x18).readU32();
          const items = list.add(0x10).readPointer();
          if (!size || items.isNull() || size > 512) continue;
          let text = '';
          for (let j = 0; j < size; j++) {
            const label = items.add(0x20 + j * Process.pointerSize).readPointer();
            if (label.isNull()) continue;
            text += readManagedString(label.add(mainTextOffset).readPointer());
          }
          if (!text.trim()) continue;
          const speaker = readManagedString(messageManager.add(nameOffset).readPointer());
          if (speaker) {
            state.speakerByManager.set(managerKey(messageManager), speaker);
            state.globalSpeaker = speaker;
          }
          emitDialogue(messageManager, text, 'CurrentMessageSnapshot');
          captured = true;
        }
        return captured;
      } catch (error) {
        report('diagnostic', {
          level: 'warning',
          where: 'snapshotCurrentDialogue',
          message: String(error),
        });
      }
      return false;
    }

    // ScriptManager parses the whole loaded scenario before playback. Capture
    // its read-only parsed message arrays so Windows can translate ahead. The
    // feature is optional: if a future game update changes these internals,
    // live SetText/AddText capture continues unchanged.
    try {
      // IL2CPP reports eleven managed parameters here (the hidden MethodInfo
      // is not included in il2cpp_class_get_method_from_name's count).
      hook('ScriptManager', 'AnalysText', 11, {
        onEnter(args) {
          this.manager = args[0];
          this.lastMessageIndexRef = args[5];
          this.talkNameRef = args[6];
          this.messageLineRef = args[7];
        },
        onLeave(retval) {
          try {
            if (!this.messageLineRef || this.messageLineRef.isNull()) return;
            if (this.messageLineRef.readU8() === 0) return;
            const index = this.lastMessageIndexRef.readS32();
            if (index < 0 || index >= SCENARIO_ARRAY_LIMIT) return;
            const nameObject = this.talkNameRef.readPointer();
            const name = readManagedString(nameObject);
            const key = managerKey(this.manager);
            let values = state.scenarioSpeakers.get(key);
            if (!values) {
              values = new Map();
              state.scenarioSpeakers.set(key, values);
            }
            // Empty is authoritative too: it marks a narration/page boundary
            // and prevents two adjacent pages from being merged together.
            values.set(index, name);
          } catch (_) {
          }
        },
      });
      hook('ScriptManager', 'AnalysScript', 2, {
        onEnter(args) {
          this.manager = args[0];
          state.scenarioSpeakers.set(managerKey(this.manager), new Map());
        },
        onLeave(retval) {
          snapshotScenarioPlan(this.manager);
        },
      });
    } catch (error) {
      report('diagnostic', {
        level: 'warning',
        where: 'ScenarioPretranslationHooks',
        message: String(error),
      });
    }

    // The name passed here is the high-level scenario speaker string and is
    // more reliable than reverse-mapping the portrait/image arguments.
    hook('CommonMessageManager', 'SetTalkName', 1, {
      onEnter(args) {
        const name = readManagedString(args[1]);
        if (name) state.globalSpeaker = name;
      },
    });

    hook('ScriptMessageCommonManager', 'SetTalkName', 4, {
      onEnter(args) {
        const manager = managerKey(args[0]);
        const characterName = readManagedString(args[3]);
        if (characterName) {
          state.speakerByManager.set(manager, characterName);
          state.globalSpeaker = characterName;
        }
      },
    });

    hook('ScriptMessageCommonManager', 'ClearTalkName', 0, {
      onEnter(args) {
        state.speakerByManager.delete(managerKey(args[0]));
        state.globalSpeaker = '';
      },
    });

    // This is the primary signal: it receives the complete page/line before
    // NGUI splits it into labels and ruby/effect fragments.
    hook('ScriptMessageCommonManager', 'SetText', 1, {
      onEnter(args) {
        emitDialogue(args[0], readManagedString(args[1]), 'ScriptMessageCommonManager.SetText');
      },
    });

    // Some scenario commands append rather than replace.  Coalesce adjacent
    // chunks from the same manager so one gradual render does not become many
    // history rows.
    hook('ScriptMessageCommonManager', 'AddText', 3, {
      onEnter(args) {
        const manager = args[0];
        const key = managerKey(manager);
        const chunk = readManagedString(args[1]);
        if (!chunk) return;
        const previous = state.pendingChunks.get(key);
        if (previous && previous.timer) clearTimeout(previous.timer);
        const combined = previous ? previous.text + chunk : chunk;
        // Preview is presentation-only: it makes the Japanese page visible to
        // Windows on the same Hook callback.  The authoritative history event
        // still waits 220 ms so split ruby/effect chunks cannot create broken
        // database rows or extra AI requests.
        emitDialoguePreview(
          manager,
          combined,
          'ScriptMessageCommonManager.AddText+Preview'
        );
        const timer = setTimeout(function () {
          state.pendingChunks.delete(key);
          emitDialogue(manager, combined, 'ScriptMessageCommonManager.AddText');
        }, 220);
        state.pendingChunks.set(key, { text: combined, timer: timer });
      },
    });

    // Story choices are managed strings before they are rendered. Capture all
    // options at the dialog boundary, including hidden and timed variants.
    hook('ScriptSelectDialog', 'Open', 3, {
      onEnter(args) {
        emitChoices(args[0], args[1], 'normal', 'ScriptSelectDialog.Open');
      },
    });

    hook('ScriptSelectDialog', 'OpenHidden', 4, {
      onEnter(args) {
        emitChoices(args[0], args[1], 'hidden', 'ScriptSelectDialog.OpenHidden');
      },
    });

    hook('ScriptSelectDialog', 'OpenLimitTime', 5, {
      onEnter(args) {
        emitChoices(args[0], args[1], 'limit_time', 'ScriptSelectDialog.OpenLimitTime');
      },
    });

    // OnClickSelect handles normal input; SelectDecide also covers a timed
    // choice decided by the game. The short dedupe window removes the normal
    // OnClickSelect -> SelectDecide double report without changing game input.
    hook('ScriptSelectDialog', 'OnClickSelect', 1, {
      onEnter(args) {
        emitChoiceSelected(args[0], args[1].toInt32(), 'ScriptSelectDialog.OnClickSelect');
      },
    });

    hook('ScriptSelectDialog', 'SelectDecide', 2, {
      onEnter(args) {
        emitChoiceSelected(args[0], args[1].toInt32(), 'ScriptSelectDialog.SelectDecide');
      },
    });

    hook('ScriptSelectDialog', 'Close', 0, {
      onEnter(args) {
        this.dialogId = managerKey(args[0]);
      },
      onLeave(retval) {
        state.choicesByDialog.delete(this.dialogId);
      },
    });

    // FGO currently uses this lower-level renderer for some event scenes.
    // `isTalkName` distinguishes the name label from the dialogue label.
    hook('ScriptLineMessage', 'SetText', 5, {
      onEnter(args) {
        const text = readManagedString(args[1]);
        const isTalkName = args[5].toInt32() !== 0;
        if (isTalkName) {
          if (text) state.globalSpeaker = text;
        } else if (state.choicesByDialog.size === 0) {
          emitDialogue(args[0], text, 'ScriptLineMessage.SetText');
        }
      },
    });

    // ScriptBackLog is populated while the scene is playing, not necessarily
    // before this Hook attaches. Re-read the complete in-game LOG whenever a
    // label is appended. Debouncing waits for the name/open-quote/body/close-
    // quote label group, so Python receives complete dialogue records only.
    hook('ScriptBackLog', 'AddLog', 1, {
      onLeave(retval) {
        scheduleBacklogSnapshot(320);
      },
    });

    const backlogCount = snapshotBacklog();
    const currentSnapshot = snapshotCurrentDialogue();
    // A late-created ScriptManager can populate its backlog just after the
    // initial snapshot. These read-only retries are signature-deduplicated.
    setTimeout(function () { snapshotBacklog(); }, 800);
    setTimeout(function () { snapshotBacklog(); }, 2000);
    state.installComplete = true;
    state.installAttempts = 0;
    report('ready', {
      arch: Process.arch,
      module_base: il2cpp.base.toString(),
      hooks: state.installed,
      current_snapshot: currentSnapshot,
      backlog_count: backlogCount,
      quest: questContext(),
    });
  } catch (error) {
    fail('install', error);
  }
}

setImmediate(install);
