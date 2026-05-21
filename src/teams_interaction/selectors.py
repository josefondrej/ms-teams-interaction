"""
Central place for Teams web selectors. Update here when the UI changes.

Teams is a React app; prefer ARIA roles and data-testid-style hooks where present.
"""

# Main scrollable message list region (fallback chain).
MESSAGE_LIST_REGION = [
    '[data-tid="message-list"]',
    '[data-scope="message-list"]',
    '[data-tid*="message-pane" i]',
    '[aria-label*="messages" i]',
    'div[data-scroll-horizontal="false"][role="presentation"]',
    'div[role="list"][aria-label]',
    'div[role="main"]',
]

# Individual message row / card roots (tried in order per candidate node).
MESSAGE_ITEM = [
    '[data-tid="chat-pane-item"]',
    '[data-tid="message-list-item"]',
    "div[data-mid]",  # message id attribute seen in some builds
    '[data-tid*="message" i][role="listitem"]',
    '[role="listitem"][data-tid]',
    '[role="article"]',
    '[data-testid="message-wrapper"]',
]

# Author display name
AUTHOR = [
    '[data-tid="message-author-name"]',
    'span[data-tid*="author"]',
    'div[data-tid="message-body-header"] span',
    'span[class*="author" i]',
]

# Visible message text body
BODY = [
    '[data-tid="messageBodyContent"]',
    '[data-tid="message-richtext"]',
    '[data-tid*="message-body" i]',
    '[data-tid*="message-content" i]',
    'div[data-tid="message-body-content"]',
    '[data-testid="message-content"]',
    '[data-testid*="message" i] [dir="auto"]',
    '[data-tid*="message" i] [dir="auto"]',
    '[role="article"] [dir="auto"]',
    'div[class*="messageBody" i]',
]

# Compose box for sending
COMPOSE = [
    'div[data-tid="ckeditor"] div[role="textbox"]',
    'div[role="textbox"][aria-label*="message" i]',
    'div[role="textbox"][contenteditable="true"]',
]

SEND_BUTTON = [
    'button[data-tid="sendButton"]',
    'button[aria-label*="Send" i]',
    'button[name="sendButton"]',
]

# Channel picker/navigation entries in Teams left rail.
CHANNEL_NAV_ITEM = [
    '[role="treeitem"]',
    '[role="option"]',
    '[role="tab"]',
    '[role="listitem"]',
    '[role="link"]',
    "button[aria-label]",
    '[data-tid*="channel"]',
    '[data-tid*="chat"]',
]

# Active channel heading / title in content pane.
# Teams uses different elements for team channels vs 1:1/group chats.
ACTIVE_CHANNEL_TITLE = [
    # Team channel header
    '[data-tid*="channel" i][data-tid*="header" i]',
    '[data-tid*="channel" i][data-tid*="title" i]',
    # 1:1 / group chat header (new Teams)
    '[data-tid="chat-header-title"]',
    '[data-tid="chat-title"]',
    'header [data-tid*="chat" i][data-tid*="title" i]',
    '[role="banner"] [data-tid*="chat" i][data-tid*="title" i]',
    # Generic heading fallbacks
    'header [role="heading"]',
    'main [role="heading"]',
    '[role="banner"] [role="heading"]',
]
