import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The body that unfolds under an expanded TaskRow: description, tickable
// checklist items, repeat/reminder labels, a project picker, and whatever
// timestamps the payload carries. Everything is optional, so the whole item
// collapses to zero height when a task has nothing more to say.
Item {
  id: root

  property var task: null
  property var projects: []
  property bool expanded: false
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  signal itemToggled(string itemId, bool done)
  signal itemAdded(string title)
  signal itemRemoved(string itemId)
  signal moveRequested(string projectId)
  signal dueRequested(string when)
  signal startRequested(string when)
  signal tagsRequested(string tags)
  signal repeatRequested(string rule)
  signal reminderRequested(string spec)

  // The panel freezes its key catcher on this: while a dropdown popup is open or a
  // field here has focus, j/k/Enter belong to this pane and not to the row cursor.
  readonly property bool editing: projectPicker.popupOpen || repeatPicker.popupOpen
    || remindPicker.popupOpen || dueField.activeFocus || startField.activeFocus
    || tagsField.activeFocus || itemField.activeFocus

  readonly property var row: task || ({})
  readonly property string description: Model.squish(row.content) || Model.squish(row.desc)
  readonly property var items: Model.asList(row.items)
  readonly property string repeatText: Model.repeatLabel(row.repeat || row.repeatFlag)
  readonly property string remindersText: Model.remindersLabel(row.reminders)
  readonly property string projectId: String(row.projectId || "")
  readonly property string taskId: String(row.id || "")
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property bool note: Model.isNote(row)

  // TickTick's own repeat presets. "Custom" is deliberately absent: an arbitrary
  // RRULE belongs in the quick-add text, not behind a dropdown that would have to
  // round-trip one it cannot spell.
  readonly property var repeatOptions: [
    { value: "", label: "Does not repeat" },
    { value: "daily", label: "Daily" },
    { value: "weekly", label: "Weekly" },
    { value: "monthly", label: "Monthly" },
    { value: "yearly", label: "Yearly" }
  ]

  readonly property var reminderOptions: [
    { value: "", label: "No reminder" },
    { value: "on time", label: "At the time" },
    { value: "5m", label: "5 minutes before" },
    { value: "15m", label: "15 minutes before" },
    { value: "30m", label: "30 minutes before" },
    { value: "1h", label: "1 hour before" },
    { value: "1d", label: "1 day before" }
  ]

  // Which preset the task currently matches, or "" when it is something we did not
  // offer — a custom RRULE stays visible in `metaLine` either way, so nothing is
  // hidden by the dropdown failing to recognise it.
  // What an untouched field shows, and therefore what committing it unchanged means.
  readonly property string dueSeed: Model.dateSeed(row.dueIso, row.isAllDay === true)
  readonly property string startSeed: Model.dateSeed(row.startIso, row.isAllDay === true)
  readonly property string tagSeed: Model.tagText(row.tags)

  readonly property string repeatValue: {
    var label = Model.repeatLabel(row.repeat || row.repeatFlag)
    for (var i = 1; i < repeatOptions.length; i++) {
      if (repeatOptions[i].label === label) return repeatOptions[i].value
    }
    return ""
  }

  // Same idea for reminders, keyed on the label the shared formatter produces. A
  // reminder we did not offer — an all-day "the day before at 09:00", or several at
  // once — reads as "No reminder" in the picker but is still spelled out in
  // `metaLine`, so nothing is hidden; only the shortcut is unavailable.
  readonly property var reminderValues: ({
    "At time": "on time",
    "5 min before": "5m",
    "15 min before": "15m",
    "30 min before": "30m",
    "1 hour before": "1h",
    "1 day before": "1d"
  })

  readonly property string reminderValue: {
    var list = Model.asList(row.reminders)
    if (list.length !== 1) return ""
    // The all-day spelling of an offset carries a clock: "1 day before, 09:00". The
    // day part is the preset; the time is TickTick's own convention for an all-day
    // reminder, not something this picker offers or needs to round-trip.
    var label = Model.reminderLabel(list[0]).replace(/,\s*\d{2}:\d{2}$/, "")
    return reminderValues[label] || ""
  }

  readonly property var projectOptions: {
    var out = []
    var list = Model.asList(projects)
    for (var i = 0; i < list.length; i++) {
      var p = list[i] || {}
      out.push({ value: String(p.id || ""), label: Model.squish(p.name) || "Untitled" })
    }
    return out
  }

  readonly property string metaLine: {
    var parts = []
    if (repeatText !== "") parts.push(repeatText)
    if (remindersText !== "") parts.push(remindersText)
    return parts.join(" · ")
  }

  readonly property string stampLine: {
    var parts = []
    var created = stamp(row.createdTime || row.created)
    var modified = stamp(row.modifiedTime || row.modified)
    if (created !== "") parts.push("Created " + created)
    if (modified !== "") parts.push("Modified " + modified)
    return parts.join(" · ")
  }

  // Always true now: the pane is where a task is edited, not merely where extra
  // fields are displayed, so there is always something to open.
  readonly property bool hasDetail: true

  // Timestamps arrive as full ISO strings with milliseconds and an offset;
  // only the calendar day is worth showing, and slicing it can't throw.
  function stamp(value) {
    var match = /^\d{4}-\d{2}-\d{2}/.exec(String(value === undefined || value === null ? "" : value))
    return match ? match[0] : ""
  }

  visible: expanded && hasDetail
  implicitHeight: visible ? body.implicitHeight + Style.spacing.md : 0
  height: implicitHeight

  // Effective visibility drops when the row collapses, and a Popup left open
  // over a row that no longer exists would keep eating keys.
  onVisibleChanged: if (!visible) {
    projectPicker.close()
    repeatPicker.close()
    remindPicker.close()
  }

  Column {
    id: body
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.leftMargin: Style.spacing.huge
    anchors.rightMargin: Style.spacing.rowPaddingX
    spacing: Style.spacing.md

    Text {
      visible: root.description !== ""
      width: parent.width
      text: root.description
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      maximumLineCount: 6
      elide: Text.ElideRight
    }

    Column {
      id: itemsColumn
      visible: root.items.length > 0
      width: parent.width
      spacing: Style.spacing.xs

      Repeater {
        model: root.items

        delegate: Item {
          id: itemRow
          required property var modelData

          readonly property string itemId: String(modelData && modelData.id ? modelData.id : "")
          readonly property bool done: !!(modelData && modelData.done)
          property bool hovering: false

          width: itemsColumn.width
          implicitHeight: itemLabel.implicitHeight + Style.spacing.xs
          height: implicitHeight

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            enabled: itemRow.itemId !== ""
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onContainsMouseChanged: itemRow.hovering = containsMouse
            onClicked: root.itemToggled(itemRow.itemId, !itemRow.done)
          }

          Text {
            id: itemGlyph
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: itemRow.done ? "󰄲" : "󰄱"
            color: itemRow.done ? root.dim : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          // Revealed under the pointer only, like the row's own delete affordance:
          // a subtask is cheap to re-add, so this needs no confirmation, but it
          // should not be a target the mouse can find by accident either.
          Text {
            id: itemRemove
            visible: itemRow.hovering && itemRow.itemId !== ""
            width: visible ? implicitWidth : 0
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: "󰅙"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption

            MouseArea {
              anchors.fill: parent
              anchors.margins: -Style.spacing.xs
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onContainsMouseChanged: if (containsMouse) itemRow.hovering = true
              onClicked: root.itemRemoved(itemRow.itemId)
            }
          }

          Text {
            id: itemLabel
            anchors.left: itemGlyph.right
            anchors.leftMargin: Style.spacing.controlGap
            anchors.right: itemRemove.left
            anchors.rightMargin: Style.spacing.xs
            anchors.verticalCenter: parent.verticalCenter
            text: Model.squish(itemRow.modelData ? itemRow.modelData.title : "") || "Untitled item"
            textFormat: Text.PlainText
            color: itemRow.done ? root.dim : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.strikeout: itemRow.done
            elide: Text.ElideRight
          }
        }
      }
    }

    // ---- add a checklist item

    TextField {
      id: itemField
      visible: !root.note
      width: parent.width
      placeholderText: "Add a subtask…"
      foreground: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      onAccepted: {
        root.itemAdded(text)
        text = ""
      }
      Keys.onEscapePressed: { text = ""; focus = false }
    }

    Text {
      visible: root.metaLine !== ""
      width: parent.width
      text: root.metaLine
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    // ---- when
    //
    // Free text, not a calendar: the same parser the quick-add box uses already
    // understands "tomorrow 5pm" and "next friday", and a date picker that could
    // not accept those would be a step backwards from typing.

    Row {
      width: parent.width
      spacing: Style.spacing.controlGap

      TextField {
        id: dueField
        width: (parent.width - Style.spacing.controlGap) / 2
        placeholderText: "Due — “tomorrow 5pm”"
        text: root.dueSeed
        foreground: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        // Empty and committed means "no due date"; that is the only way to clear
        // one without a separate button nobody would find.
        onAccepted: { root.dueRequested(text); focus = false }
        Keys.onEscapePressed: { text = root.dueSeed; focus = false }
      }

      TextField {
        id: startField
        width: (parent.width - Style.spacing.controlGap) / 2
        placeholderText: "Starts…"
        text: root.startSeed
        foreground: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        onAccepted: { root.startRequested(text); focus = false }
        Keys.onEscapePressed: { text = root.startSeed; focus = false }
      }
    }

    TextField {
      id: tagsField
      width: parent.width
      placeholderText: "Tags — “#work #urgent”"
      text: root.tagSeed
      foreground: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      onAccepted: { root.tagsRequested(text); focus = false }
      Keys.onEscapePressed: { text = root.tagSeed; focus = false }
    }

    Row {
      width: parent.width
      spacing: Style.spacing.controlGap

      Dropdown {
        id: repeatPicker
        width: (parent.width - Style.spacing.controlGap) / 2
        label: "Repeat"
        showLabel: false
        options: root.repeatOptions
        foreground: root.foreground
        fontFamily: root.fontFamily
        onChanged: function (value) { root.repeatRequested(value) }
      }

      Dropdown {
        id: remindPicker
        width: (parent.width - Style.spacing.controlGap) / 2
        label: "Remind"
        showLabel: false
        options: root.reminderOptions
        foreground: root.foreground
        fontFamily: root.fontFamily
        onChanged: function (value) { root.reminderRequested(value) }
      }
    }

    Dropdown {
      id: projectPicker
      visible: root.projectOptions.length > 1
      width: parent.width
      label: "Project"
      showLabel: false
      options: root.projectOptions
      foreground: root.foreground
      fontFamily: root.fontFamily
      onChanged: function (value) { root.moveRequested(value) }
    }

    // Selecting an option assigns Dropdown.value imperatively, which would
    // otherwise strand the trigger label on the old project forever.
    Binding {
      target: projectPicker
      property: "value"
      value: root.projectId
    }

    Binding {
      target: repeatPicker
      property: "value"
      value: root.repeatValue
    }

    // Reminders are a list and the picker offers one; showing the first is honest
    // enough, and `metaLine` above spells out "+2" when there are more.
    Binding {
      target: remindPicker
      property: "value"
      value: root.reminderValue
    }

    // `when: !activeFocus` is what makes these fields editable at all: a plain
    // `text:` binding is destroyed by the first keystroke, so the field would then
    // never follow the task again. Restated whenever focus leaves, so a refresh —
    // or another device's edit — is reflected the moment the user stops typing.
    Binding {
      target: dueField
      property: "text"
      value: root.dueSeed
      when: !dueField.activeFocus
    }

    Binding {
      target: startField
      property: "text"
      value: root.startSeed
      when: !startField.activeFocus
    }

    Binding {
      target: tagsField
      property: "text"
      value: root.tagSeed
      when: !tagsField.activeFocus
    }

    Text {
      visible: root.stampLine !== ""
      width: parent.width
      text: root.stampLine
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }
}
