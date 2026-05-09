"""
Central place for Teams web selectors. Update here when the UI changes.

Teams is a React app; prefer ARIA roles and data-testid-style hooks where present.
"""

# Main scrollable message list region (fallback chain).
MESSAGE_LIST_REGION = [
    '[data-tid="message-list"]',
    'div[data-scroll-horizontal="false"][role="presentation"]',
    'div[role="main"]',
]

# Individual message row / card roots (tried in order per candidate node).
MESSAGE_ITEM = [
    '[data-tid="chat-pane-item"]',
    "div[data-mid]",  # message id attribute seen in some builds
    '[data-testid="message-wrapper"]',
]

# Author display name
AUTHOR = [
    '[data-tid="message-author-name"]',
    'span[data-tid*="author"]',
    'div[data-tid="message-body-header"] span',
]

# Visible message text body
BODY = [
    '[data-tid="messageBodyContent"]',
    'div[data-tid="message-body-content"]',
    '[data-testid="message-content"]',
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
