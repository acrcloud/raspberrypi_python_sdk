#ifndef _Included_acr_extr_tool_H
#define _Included_acr_extr_tool_H

#ifdef __cplusplus
extern "C" {
#endif

int create_fingerprint(char *pcm_buffer, int pcm_buffer_len, char is_db_fingerprint, char **fps_buffer);
void acr_free(char *fps_buffer);

#ifdef __cplusplus
}
#endif

#endif
