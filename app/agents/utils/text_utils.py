"""Text utility functions for handling text normalization and fragmentation detection."""


def has_fragmentation(text):
    """Check if text has fragmentation patterns indicating incomplete audio transcription.
    
    Fragmented transcriptions often have:
    - Double spaces
    - Single letter words (except common ones like "a", "i")
    - Spaces within words (like "cre ate" instead of "create")
    - Spaces around punctuation (like "4 :00" or "a .m." instead of "4:00" or "a.m.")
    
    Args:
        text: The text to check for fragmentation
        
    Returns:
        bool: True if fragmentation patterns are detected
    """
    words = text.split()
    # Check for patterns that indicate fragmentation
    has_double_spaces = "  " in text
    has_spaces_in_words = any(" " in word for word in words)
    
    # Check for single letter words (excluding common ones like "a", "i", "I")
    common_single_letters = {"a", "i", "A", "I"}
    has_single_letter_words = any(
        len(word) == 1 and word.isalpha() and word not in common_single_letters 
        for word in words
    )
    
    # Check for spaces around punctuation/numbers (like "4 :00", "a .m.", ":00 a .m.")
    has_spaces_around_punctuation = (
        " :" in text or  # Space before colon
        " ." in text or  # Space before period
        ": " in text and " :" in text  # Both patterns present
    )
    
    return has_double_spaces or has_single_letter_words or has_spaces_in_words or has_spaces_around_punctuation


def normalize_text(text):
    """Normalize text for comparison by lowercasing and removing extra whitespace.
    
    Args:
        text: The text to normalize
        
    Returns:
        str: Normalized text (lowercase, single spaces, trimmed)
    """
    if not text:
        return ""
    return " ".join(text.lower().strip().split())


def should_skip_fragmented_entry(entry_content, final_user_input):
    """Determine if a scratchpad entry should be skipped because it's a fragmented transcription.
    
    Fragmented audio transcriptions should be skipped when we have a complete final user input
    to avoid creating duplicate or incorrect tasks.
    
    Args:
        entry_content: The content from the scratchpad entry
        final_user_input: The final, complete user input being processed
        
    Returns:
        bool: True if the entry should be skipped
    """
    if not entry_content or not final_user_input:
        return False
    
    # Normalize for comparison (lowercase, remove extra spaces)
    entry_normalized = normalize_text(entry_content)
    final_normalized = normalize_text(final_user_input)
    
    if not entry_normalized or not final_normalized:
        return False
    
    # Check for fragmentation patterns
    entry_has_fragmentation = has_fragmentation(entry_content)
    final_has_fragmentation = has_fragmentation(final_user_input)
    
    # Skip if entry is a fragment that's a substring of the final user input
    if entry_has_fragmentation and entry_normalized in final_normalized:
        return True
    
    # Skip if entry is much shorter (clearly incomplete)
    if entry_has_fragmentation and len(entry_normalized) < len(final_normalized) * 0.7:
        return True
    
    # Skip if entry has fragmentation but final input is complete and they're similar in length
    # This catches cases where the fragment is mis-transcribed (like "ck my ra nge" vs "pack my rain jacket")
    if entry_has_fragmentation and not final_has_fragmentation:
        # If lengths are similar (within 30%), likely the same request with different transcription quality
        length_ratio = len(entry_normalized) / len(final_normalized) if final_normalized else 0
        if 0.7 <= length_ratio <= 1.3:
            # Similar length, but entry is fragmented and final is complete - skip the fragment
            return True
    
    return False
