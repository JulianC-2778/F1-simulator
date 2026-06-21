#ifndef _PLAYER_LOGGER_H_
#define _PLAYER_LOGGER_H_
#include <track.h>
#include <car.h>
#include <raceman.h>
void PlayerLoggerStart(int playerIndex, tTrack *track, tCarElt *car, tSituation *s);
void PlayerLoggerSample(int playerIndex, const tCarElt *car, const tSituation *s);
void PlayerLoggerStop(int playerIndex);
#endif
